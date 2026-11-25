"""Cloud Run provider：把 Cloud Run jobs 当成一台按需拉起的 calltable 主机。

模型
----
* 配置里 ``provider: "cloudrun"`` 的主机没有常驻执行器。hub 的 janitor 每轮
  调用 :class:`Provisioner`，在**同一套** slot / 锁规则下替这台主机 claim
  任务（worker 先写成 ``pending:<id>`` 占位），然后执行一次 Cloud Run job
  execution，把任务 id 通过环境变量传进去。
* 容器里跑 ``calltable exec --once --job-id <id>``：通过
  ``POST /api/exec/jobs/<id>/attach`` 认领这条预留任务，之后与普通执行器完全
  一样（拉 bundle、跑命令、传日志/产物、上报终态），跑完即退出，容器回收。
* 每个 (项目, slot) 对应一个 Cloud Run job 定义（镜像按项目配置，CPU/内存按
  slot 档位），``maxRetries=0``：calltable 已经记过终态的任务不能被 Cloud Run
  再跑一遍。
* 对账：execution 结束但任务还在 running → 记 error；任务已终结但 execution
  还在跑 → 取消 execution；拉起后 ``attach_timeout`` 内没人认领 → 取消并记
  error；认领后超过 ``heartbeat_timeout`` 没心跳 → 同上。
* 花费封顶：按 execution 的存活时间 × (vCPU 单价 × vCPU + 内存单价 × GiB) 估算
  本自然月（UTC）花费，最少按 60 秒计；达到 ``monthly_budget_usd`` 后不再拉起
  新 execution，任务继续排队（``on_budget_exceeded: "queue"``，默认）或直接
  失败（``"fail"``）。这是保守的上限估算，不读真实账单、也不扣免费额度。

只用标准库：对 Cloud Run 的调用通过 ``gcloud`` 命令行完成（hub 所在机器上的
ADC / 服务账号身份），测试里替换成假的 :class:`CloudRunAPI`。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .db import PENDING_WORKER_PREFIX, QUEUED, RUNNING, TERMINAL
from .util import now, parse_duration

PROVIDER = "cloudrun"

#: us-central1 Tier 1 默认单价（USD，https://cloud.google.com/run/pricing，
#: jobs / instance-based billing）。别的区域在 host 配置里覆盖。
DEFAULT_CPU_PRICE = 0.000018      # per vCPU-second
DEFAULT_MEMORY_PRICE = 0.000002   # per GiB-second
#: Cloud Run jobs 计费的最小时长。
MIN_BILLED_SECONDS = 60

#: slot 档位默认资源：light 适合单元测试，heavy 是 Cloud Run jobs 的上限附近
#: （8 vCPU / 32 GiB），给大型项目的全量构建和集成测试用。
DEFAULT_RESOURCES = {
    "light": {"cpu": 2, "memory": "8Gi"},
    "heavy": {"cpu": 8, "memory": "32Gi"},
}

DEFAULTS = {
    "region": "us-central1",
    "monthly_budget_usd": 60.0,
    "on_budget_exceeded": "queue",
    "max_task_timeout": "3h",
    "timeout_grace": "5m",
    "attach_timeout": "15m",
    "heartbeat_timeout": "15m",
    "reconcile_interval": "60s",
    "job_prefix": "ct",
    "repository": "calltable",
    "hub_token_secret": "calltable-hub-token",
    "tailscale_authkey_secret": "calltable-tailscale-authkey",
}

_MEMORY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(gi|g|mi|m)?\s*$", re.IGNORECASE)


class CloudRunError(RuntimeError):
    pass


def memory_gib(text: str | int | float) -> float:
    """``"32Gi"`` / ``"512Mi"`` / ``8`` (GiB) → GiB。"""
    if isinstance(text, (int, float)):
        return float(text)
    match = _MEMORY_RE.match(str(text))
    if not match:
        raise ValueError(f"bad memory size {text!r} (use e.g. 8Gi, 512Mi)")
    value = float(match.group(1))
    unit = (match.group(2) or "gi").lower()
    if unit in ("mi", "m"):
        return value / 1024.0
    return value


def sanitize_name(text: str, limit: int = 63) -> str:
    """Cloud Run 资源名：小写字母开头，只含 ``[a-z0-9-]``，≤63 字符。"""
    name = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    if not name or not name[0].isalpha():
        name = "x-" + name
    return name[:limit].rstrip("-")


def month_start(ts: float) -> float:
    moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime(moment.year, moment.month, 1, tzinfo=timezone.utc).timestamp()


@dataclass
class CloudRunSettings:
    """一台 ``provider: cloudrun`` 主机的配置（``hosts.<name>.cloudrun``）。"""

    host: str
    project: str
    region: str
    service_account: str = ""
    registry: str = ""
    hub_url: str = ""
    job_prefix: str = "ct"
    resources: dict = field(default_factory=dict)
    monthly_budget_usd: float = 60.0
    on_budget_exceeded: str = "queue"
    cpu_price: float = DEFAULT_CPU_PRICE
    memory_price: float = DEFAULT_MEMORY_PRICE
    max_task_timeout: int = 3 * 3600
    timeout_grace: int = 300
    attach_timeout: int = 900
    heartbeat_timeout: int = 900
    reconcile_interval: int = 60
    hub_token_secret: str = "calltable-hub-token"
    tailscale_authkey_secret: str = "calltable-tailscale-authkey"
    enabled: bool = True
    #: hub 机器上的 gcloud 可执行文件（默认在 PATH 里找）。
    gcloud: str = ""

    @classmethod
    def from_host(cls, host: str, raw: dict, *, hub_url: str = "") -> "CloudRunSettings":
        data = dict(DEFAULTS)
        data.update(raw or {})
        project = str(data.get("project") or "").strip()
        if not project:
            raise ValueError(f"host {host}: cloudrun.project is required")
        region = str(data.get("region") or DEFAULTS["region"]).strip()
        resources = {slot: dict(spec) for slot, spec in DEFAULT_RESOURCES.items()}
        for slot, spec in (data.get("resources") or {}).items():
            resources.setdefault(str(slot), {}).update(dict(spec or {}))
        action = str(data.get("on_budget_exceeded") or "queue").lower()
        if action not in ("queue", "fail"):
            raise ValueError(f"host {host}: on_budget_exceeded must be queue or fail")
        repository = str(data.get("repository") or DEFAULTS["repository"])
        registry = str(data.get("registry") or
                       f"{region}-docker.pkg.dev/{project}/{repository}")
        return cls(
            host=host, project=project, region=region,
            service_account=str(data.get("service_account") or ""),
            registry=registry.rstrip("/"),
            hub_url=str(data.get("hub_url") or hub_url or "").rstrip("/"),
            job_prefix=sanitize_name(str(data.get("job_prefix") or "ct"), 20),
            resources=resources,
            monthly_budget_usd=float(data.get("monthly_budget_usd")),
            on_budget_exceeded=action,
            cpu_price=float(data.get("cpu_price", DEFAULT_CPU_PRICE)),
            memory_price=float(data.get("memory_price", DEFAULT_MEMORY_PRICE)),
            max_task_timeout=parse_duration(data.get("max_task_timeout")),
            timeout_grace=parse_duration(data.get("timeout_grace")),
            attach_timeout=parse_duration(data.get("attach_timeout")),
            heartbeat_timeout=parse_duration(data.get("heartbeat_timeout")),
            reconcile_interval=parse_duration(data.get("reconcile_interval")),
            hub_token_secret=str(data.get("hub_token_secret")),
            tailscale_authkey_secret=str(data.get("tailscale_authkey_secret")),
            enabled=bool(data.get("enabled", True)),
            gcloud=str(data.get("gcloud") or ""),
        )

    # ---- 资源 / 命名
    def slot_resources(self, slot: str) -> tuple[float, float]:
        spec = self.resources.get(slot) or self.resources.get("light") or {}
        return float(spec.get("cpu", 2)), memory_gib(spec.get("memory", "8Gi"))

    def job_name(self, project: str, slot: str) -> str:
        return sanitize_name(f"{self.job_prefix}-{project}-{slot}")

    def task_timeout(self, job_timeout: int | None) -> int:
        wanted = int(job_timeout or self.max_task_timeout) + self.timeout_grace
        return max(60, min(wanted, self.max_task_timeout))

    def cost(self, seconds: float, vcpu: float, mem_gib: float) -> float:
        billed = max(float(MIN_BILLED_SECONDS), float(seconds))
        return billed * (vcpu * self.cpu_price + mem_gib * self.memory_price)


def project_image(settings: CloudRunSettings, project: str,
                  profile_cloudrun: dict | None) -> str:
    """项目镜像：profile ``cloudrun.image`` 优先，否则 ``<registry>/<project>:latest``。"""
    image = str((profile_cloudrun or {}).get("image") or "").strip()
    if image:
        return image
    return f"{settings.registry}/{sanitize_name(project)}:latest"


# ------------------------------------------------------------------ API 层
class CloudRunAPI:
    """Cloud Run 调用的最小接口；默认实现走 ``gcloud``。"""

    def run_job(self, settings: CloudRunSettings, job_name: str, *,
                env: dict[str, str], task_timeout: int) -> str:
        raise NotImplementedError

    def describe_execution(self, settings: CloudRunSettings,
                           execution: str) -> dict:
        raise NotImplementedError

    def cancel_execution(self, settings: CloudRunSettings, execution: str) -> None:
        raise NotImplementedError


class GcloudCloudRunAPI(CloudRunAPI):
    """用 hub 机器上的 ``gcloud``（ADC 或服务账号登录）调用 Cloud Run Admin API。"""

    def __init__(self, gcloud: str | None = None, timeout: float = 120):
        self.gcloud = gcloud or shutil.which("gcloud") or "gcloud"
        self.timeout = timeout

    def _run(self, settings: CloudRunSettings, args: list[str], *,
             with_stderr: bool = False) -> str:
        binary = settings.gcloud or self.gcloud
        try:
            proc = subprocess.run([binary, *args], capture_output=True,
                                  text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CloudRunError(f"gcloud failed to start: {exc}") from exc
        if proc.returncode != 0:
            raise CloudRunError(
                f"gcloud {' '.join(args[:4])} exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:500]}")
        if with_stderr:
            return (proc.stdout or "") + "\n" + (proc.stderr or "")
        return proc.stdout

    def run_job(self, settings, job_name, *, env, task_timeout):
        args = ["run", "jobs", "execute", job_name,
                f"--project={settings.project}", f"--region={settings.region}",
                "--async", f"--task-timeout={int(task_timeout)}s",
                "--format=json"]
        if env:
            args.append("--update-env-vars=" + ",".join(
                f"{key}={value}" for key, value in sorted(env.items())))
        out = self._run(settings, args, with_stderr=True)
        data = {}
        start = out.find("{")
        if start >= 0:
            try:
                data, _ = json.JSONDecoder().raw_decode(out[start:])
            except ValueError:
                data = {}
        name = (data.get("metadata") or {}).get("name") or data.get("name") or ""
        if not name:
            match = re.search(r"[Ee]xecution \[?([a-z0-9-]+)\]?", out or "")
            name = match.group(1) if match else ""
        if not name:
            raise CloudRunError(f"could not parse execution name from: {out[:300]}")
        return name.rsplit("/", 1)[-1]

    def describe_execution(self, settings, execution):
        out = self._run(settings, ["run", "jobs", "executions", "describe", execution,
                         f"--project={settings.project}",
                         f"--region={settings.region}", "--format=json"])
        return json.loads(out or "{}")

    def cancel_execution(self, settings, execution):
        self._run(settings, ["run", "jobs", "executions", "cancel", execution,
                   f"--project={settings.project}", f"--region={settings.region}",
                   "--quiet"])


def execution_finished(info: dict) -> tuple[bool, str]:
    """从 execution 描述里判断是否结束；返回 (结束, 状态说明)。"""
    status = info.get("status") or {}
    if status.get("completionTime"):
        succeeded = int(status.get("succeededCount") or 0)
        failed = int(status.get("failedCount") or 0)
        cancelled = int(status.get("cancelledCount") or 0)
        return True, (f"succeeded={succeeded} failed={failed} "
                      f"cancelled={cancelled}")
    for condition in status.get("conditions") or []:
        if (condition.get("type") == "Completed"
                and condition.get("status") in ("True", "False")):
            return True, str(condition.get("reason") or condition.get("message")
                             or "completed")
    return False, ""


def parse_rfc3339(value: str | None) -> float | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ provisioner
class Provisioner:
    """hub 侧：为 cloudrun 主机拉起、对账、封顶 Cloud Run executions。"""

    def __init__(self, hub, api: CloudRunAPI | None = None):
        self.hub = hub
        self.store = hub.store
        self.api = api or GcloudCloudRunAPI()
        self._lock = threading.Lock()
        self._last_reconcile: dict[str, float] = {}
        self._budget_warned: dict[str, float] = {}
        self.settings: dict[str, CloudRunSettings] = {}
        for name, spec in hub.config.hosts.items():
            if spec.provider != PROVIDER:
                continue
            try:
                self.settings[name] = CloudRunSettings.from_host(
                    name, spec.provider_config, hub_url=hub.config.public_url)
            except ValueError as exc:
                hub._warn(f"cloudrun host {name} disabled: {exc}")

    @property
    def hosts(self) -> list[str]:
        return list(self.settings)

    # ---- 花费
    def month_spend(self, host: str, at: float | None = None) -> float:
        settings = self.settings[host]
        at = at or now()
        start = month_start(at)
        total = 0.0
        for row in self.store.cloud_executions(since=start):
            if row["host"] != host:
                continue
            begin = max(float(row["launched"]), start)
            end = float(row["ended"]) if row["ended"] else at
            if end < start:
                continue
            total += settings.cost(end - begin, row["vcpu"], row["memory_gib"])
        return round(total, 4)

    def summary(self) -> dict:
        result = {}
        at = now()
        for host, settings in self.settings.items():
            active = [row for row in self.store.cloud_executions(active_only=True)
                      if row["host"] == host]
            spend = self.month_spend(host, at)
            result[host] = {
                "project": settings.project,
                "region": settings.region,
                "enabled": settings.enabled,
                "month_start": month_start(at),
                "month_spend_usd": spend,
                "monthly_budget_usd": settings.monthly_budget_usd,
                "over_budget": spend >= settings.monthly_budget_usd,
                "on_budget_exceeded": settings.on_budget_exceeded,
                "active_executions": len(active),
                "executions": active,
            }
        return result

    # ---- 主循环
    def tick(self) -> None:
        with self._lock:
            for host, settings in self.settings.items():
                try:
                    self._reconcile(host, settings)
                except Exception as exc:  # pragma: no cover - 兜底
                    print(f"[cloudrun] reconcile {host}: {type(exc).__name__}: "
                          f"{exc}", flush=True)
                if not settings.enabled:
                    continue
                try:
                    self._provision(host, settings)
                except Exception as exc:  # pragma: no cover - 兜底
                    print(f"[cloudrun] provision {host}: {type(exc).__name__}: "
                          f"{exc}", flush=True)

    def _provision(self, host: str, settings: CloudRunSettings) -> None:
        if not self._has_queued(host):
            return
        spend = self.month_spend(host)
        if spend >= settings.monthly_budget_usd:
            self._over_budget(host, settings, spend)
            return
        while True:
            # 让 hub 的 claim 认为这台主机「在线」；调度规则（slot、锁、drain）
            # 与常驻主机完全相同。
            self.store.touch_host(host)
            worker = f"{PENDING_WORKER_PREFIX}{uuid.uuid4().hex[:12]}"
            job = self.store.claim(host, worker)
            if job is None:
                return
            self._launch(host, settings, job)
            if self.month_spend(host) >= settings.monthly_budget_usd:
                return

    def _has_queued(self, host: str) -> bool:
        for job in self.store.jobs(status=QUEUED, limit=10000):
            if host in (job.get("targets") or [job.get("host")]):
                return True
        return False

    def _over_budget(self, host: str, settings: CloudRunSettings,
                     spend: float) -> None:
        month = month_start(now())
        message = (f"cloudrun host {host}: estimated spend ${spend:.2f} reached "
                   f"the monthly cap ${settings.monthly_budget_usd:.2f}")
        if self._budget_warned.get(host) != month:
            self._budget_warned[host] = month
            print(f"[cloudrun] {message}; not launching new executions",
                  flush=True)
        if settings.on_budget_exceeded != "fail":
            return
        for job in self.store.jobs(status=QUEUED, limit=10000):
            targets = job.get("targets") or [job.get("host")]
            if targets == [host]:
                self.store.finish(job["id"], status="error", exit_code=None,
                                  error=message,
                                  cleanup_at=now() + self.hub.config.retention_failure)

    def _launch(self, host: str, settings: CloudRunSettings, job: dict) -> None:
        profile = self.hub.hub_profile(job)
        cloud_job = settings.job_name(job["project"], job["slot"])
        vcpu, mem = settings.slot_resources(job["slot"])
        self.store.cloud_record_launch(job["id"], host, cloud_job=cloud_job,
                                       vcpu=vcpu, memory_gib=mem)
        env = {"CALLTABLE_JOB_ID": job["id"], "CALLTABLE_EXEC_HOST": host}
        if settings.hub_url:
            env["CALLTABLE_HUB_URL"] = settings.hub_url
        try:
            execution = self.api.run_job(
                settings, cloud_job, env=env,
                task_timeout=settings.task_timeout(job.get("timeout")))
        except Exception as exc:
            note = f"cloud run launch failed ({cloud_job}): {exc}"
            self.store.cloud_update(job["id"], state="failed", ended=now(),
                                    note=note[:500])
            self.store.finish(job["id"], status="error", exit_code=None,
                              error=note[:1000],
                              cleanup_at=now() + self.hub.config.retention_failure)
            print(f"[cloudrun] {job['id']}: {note}", flush=True)
            return
        self.store.cloud_update(job["id"], execution=execution, state="launched")
        image = project_image(settings, job["project"],
                              profile.cloudrun if profile else {})
        print(f"[cloudrun] {job['id']} -> {cloud_job} execution {execution} "
              f"(image {image})", flush=True)

    def _reconcile(self, host: str, settings: CloudRunSettings) -> None:
        current = now()
        describe = (current - self._last_reconcile.get(host, 0.0)
                    >= settings.reconcile_interval)
        if describe:
            self._last_reconcile[host] = current
        for row in self.store.cloud_executions(active_only=True):
            if row["host"] != host:
                continue
            job = self.store.job(row["job_id"])
            job_done = job is None or job.get("status") in TERMINAL
            execution = row["execution"]
            if not execution:
                if job_done:
                    self.store.cloud_update(row["job_id"], state="ended",
                                            ended=current)
                continue
            finished, detail, ended_at = False, "", None
            if describe or job_done:
                try:
                    info = self.api.describe_execution(settings, execution)
                    finished, detail = execution_finished(info)
                    ended_at = parse_rfc3339(
                        (info.get("status") or {}).get("completionTime"))
                except Exception as exc:
                    print(f"[cloudrun] describe {execution}: {exc}", flush=True)
            if finished:
                self.store.cloud_update(row["job_id"], state="ended",
                                        ended=ended_at or current,
                                        note=detail[:500])
                if not job_done and job.get("status") == RUNNING:
                    self._fail(job, f"cloud run execution {execution} ended "
                                    f"before the job reported ({detail})")
                continue
            if job_done:
                finished_at = float(job.get("finished") or current) if job else current
                if current - finished_at >= settings.timeout_grace:
                    self._cancel(row, settings, "job already finished")
                continue
            attached = row["attached"]
            if not attached and current - row["launched"] >= settings.attach_timeout:
                self._cancel(row, settings, "no executor attached within "
                             f"{settings.attach_timeout}s")
                self._fail(job, f"cloud run execution {execution} never attached "
                                f"within {settings.attach_timeout}s")
                continue
            seen = row["last_seen"] or attached
            if attached and seen and current - seen >= settings.heartbeat_timeout:
                self._cancel(row, settings, "executor heartbeat lost")
                self._fail(job, f"cloud run execution {execution}: executor "
                                f"heartbeat lost for {int(current - seen)}s")

    def _cancel(self, row: dict, settings: CloudRunSettings, reason: str) -> None:
        try:
            self.api.cancel_execution(settings, row["execution"])
        except Exception as exc:
            print(f"[cloudrun] cancel {row['execution']}: {exc}", flush=True)
        self.store.cloud_update(row["job_id"], state="cancelled", ended=now(),
                                note=reason[:500])

    def _fail(self, job: dict | None, message: str) -> None:
        if not job or job.get("status") in TERMINAL:
            return
        self.store.finish(job["id"], status="error", exit_code=None,
                          error=message,
                          cleanup_at=now() + self.hub.config.retention_failure)
        print(f"[cloudrun] {job['id']}: {message}", flush=True)
