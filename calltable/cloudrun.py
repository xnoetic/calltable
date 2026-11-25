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
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import gitops
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
    "monthly_budget_usd": 120.0,
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
    monthly_budget_usd: float = 120.0
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
    #: profile ``cloudrun.git`` 没写 ``token_secret`` 键时用的默认 secret 名
    #: （多个项目共用一个只读 token）。
    git_token_secret: str = ""
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
        git_token_secret = str(data.get("git_token_secret") or "").strip()
        if git_token_secret and not _SECRET_NAME_RE.fullmatch(git_token_secret):
            raise ValueError(f"host {host}: git_token_secret {git_token_secret!r} "
                             "is not a valid Secret Manager secret name")
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
            git_token_secret=git_token_secret,
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


# ------------------------------------------------------------------ git 中转
#: 远端仓库 URL / 推送 URL：会进 argv、``--update-env-vars``（逗号分隔）、
#: cloudbuild.yaml 和 bash 单引号里，只放行不需要转义的字符。
_GIT_URL_RE = re.compile(r"^[A-Za-z0-9/][A-Za-z0-9._~:/@%+-]*$")
#: 支持的 URL 形式（都不允许带口令；http(s) 连用户名都不允许）。
_HTTP_URL_RE = re.compile(r"^(https?)://([^/]*)(/.*)?$", re.IGNORECASE)
_SSH_URL_RE = re.compile(r"^ssh://(?:([^/@:]+)@)?([^/@:]+)(?::(\d+))?(/.*)$",
                         re.IGNORECASE)
_SCP_URL_RE = re.compile(r"^(?:([A-Za-z0-9._-]+)@)?([A-Za-z0-9.-]+):([^:@]+)$")
_FILE_URL_RE = re.compile(r"^file://(/.*)$", re.IGNORECASE)
_GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: hub 推送临时 ref 的超时（秒）。provisioner 线程是串行的，必须有界；它发生在
#: execution 拉起之前，也算进 attach 超时里，所以要短。
GIT_PUSH_TIMEOUT = 120.0
#: 删除临时 ref / 删除失败后 ls-remote 核对的超时（秒）。
GIT_DELETE_TIMEOUT = 60.0
GIT_LS_REMOTE_TIMEOUT = 30.0
#: 某个 push_url 删除失败后，至少隔这么久再碰这个远端（秒）。
GIT_CLEANUP_RETRY = 60.0
#: 一轮对账最多处理多少条待删记录，避免清理长时间占住 provisioner 线程。
GIT_CLEANUP_BATCH = 10
#: 容器里预置镜像的目录（``ct cloudrun image`` 在 Cloud Build 里 clone 进去）。
BAKED_MIRROR_ROOT = "/opt/calltable/mirrors"


@dataclass
class GitRelay:
    """profile ``cloudrun.git``：经远端仓库（GitHub）给容器中转代码。"""

    url: str
    push_url: str
    branch: str = "main"
    token_secret: str = ""

    @classmethod
    def from_profile(cls, profile_cloudrun: dict | None,
                     default_token_secret: str = "") -> "GitRelay | None":
        """没有 ``cloudrun.git.url`` 返回 None（功能关闭）；配置不合法抛 ValueError。

        ``token_secret``：profile 里有这个键就用它（空串 / null 表示不注入
        token）；没有这个键才用主机级默认 ``git_token_secret``。
        """
        raw = (profile_cloudrun or {}).get("git")
        if not raw:
            return None
        if not isinstance(raw, dict):
            raise ValueError("cloudrun.git must be an object")
        url = str(raw.get("url") or "").strip()
        if not url:
            return None
        relay = cls(url=url,
                    push_url=str(raw.get("push_url") or "").strip() or url,
                    branch=str(raw.get("branch") or "main").strip(),
                    token_secret=(str(raw.get("token_secret") or "").strip()
                                  if "token_secret" in raw
                                  else str(default_token_secret or "").strip()))
        relay.validate()
        return relay

    def validate(self) -> None:
        for name, value in (("url", self.url), ("push_url", self.push_url)):
            if not _GIT_URL_RE.fullmatch(value):
                raise ValueError(
                    f"cloudrun.git.{name} {redact_url(value)!r} contains characters "
                    "that are "
                    "not allowed (whitespace, quotes, shell or YAML metacharacters, "
                    "commas) or starts with '-'")
        # 容器只能用 token 走 https 取代码，而可见性检查也只对 http(s) 有效：
        # url 必须是 http(s)；本地路径只用于测试和本机环境（不可能是公网仓库）。
        if url_kind(self.url, "url") not in ("http", "local"):
            raise ValueError(f"cloudrun.git.url {self.url!r} must be an http(s):// "
                             "URL (or a local path / file:// URL)")
        # 只有 url 做可见性检查：push_url 必须指向同一个仓库。
        if repo_identity(self.url, "url") != repo_identity(self.push_url, "push_url"):
            raise ValueError(f"cloudrun.git.push_url {self.push_url!r} does not name "
                             f"the same repository as cloudrun.git.url {self.url!r}")
        if (not _GIT_BRANCH_RE.fullmatch(self.branch) or ".." in self.branch
                or self.branch.endswith((".lock", "/", "."))):
            raise ValueError(f"cloudrun.git.branch {self.branch!r} is not a valid "
                             "branch name")
        if self.token_secret and not _SECRET_NAME_RE.fullmatch(self.token_secret):
            raise ValueError(f"cloudrun.git.token_secret {self.token_secret!r} is not "
                             "a valid Secret Manager secret name")


def redact_url(url: str) -> str:
    """报错里引用 URL 时打码凭据：http(s) 的整个 userinfo，其余形式的口令。"""
    url = re.sub(r"^(https?://)[^/@]*@", r"\1***@", url, flags=re.IGNORECASE)
    return re.sub(r"(^|://)([^/@:]*):[^/@]*@", r"\1\2:***@", url)


def url_kind(url: str, name: str = "url") -> str:
    """``http`` / ``ssh`` / ``local``；不支持或带凭据的 URL 抛 ValueError。"""
    match = _HTTP_URL_RE.match(url)
    if match:
        if "@" in match.group(2):
            raise ValueError(f"cloudrun.git.{name} must not contain credentials or a "
                             "user name; use token_secret (container) or the hub's "
                             "own git credentials (push)")
        if not match.group(2):
            raise ValueError(f"cloudrun.git.{name} {redact_url(url)!r} has no host")
        return "http"
    if url.lower().startswith("ssh://"):
        match = _SSH_URL_RE.match(url)
        if not match:
            raise ValueError(f"cloudrun.git.{name} {redact_url(url)!r} is not a "
                             "valid ssh URL "
                             "(a password in the URL is not allowed)")
        return "ssh"
    # Windows 盘符路径（C:/...）只在 Windows 上算本地路径；别处 git 把它当 scp 地址。
    if (_FILE_URL_RE.match(url) or url.startswith("/")
            or (os.name == "nt" and re.match(r"^[A-Za-z]:/", url))):
        return "local"
    if "://" not in url:
        if _SCP_URL_RE.match(url):
            return "ssh"
        raise ValueError(f"cloudrun.git.{name} {redact_url(url)!r} is not a "
                         "valid scp-like "
                         "address (user@host:path; a password is not allowed)")
    raise ValueError(f"cloudrun.git.{name} {redact_url(url)!r}: unsupported URL "
                     "scheme")


def _repo_path(path: str) -> str:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4].rstrip("/")
    return path


def repo_identity(url: str, name: str = "url") -> tuple[str, str]:
    """把各种写法归一成 ``(主机, 路径)``，用来判断两个 URL 是不是同一个仓库。

    主机小写、去掉端口和用户名；路径去掉首尾斜杠和 ``.git``。本地路径主机为空，
    路径经 ``normpath`` 规范化。
    """
    kind = url_kind(url, name)
    if kind == "http":
        match = _HTTP_URL_RE.match(url)
        host = match.group(2).lower().split(":", 1)[0]  # 去掉端口
        return host, _repo_path(match.group(3) or "")
    if kind == "ssh":
        match = _SSH_URL_RE.match(url)
        if match:
            return match.group(2).lower(), _repo_path(match.group(4))
        match = _SCP_URL_RE.match(url)
        return match.group(2).lower(), _repo_path(match.group(3))
    match = _FILE_URL_RE.match(url)
    path = match.group(1) if match else url
    path = os.path.normpath(path).replace("\\", "/")
    if path.endswith(".git"):
        path = path[:-4]
    return "", path.rstrip("/") or "/"


def is_local_url(url: str) -> bool:
    try:
        return url_kind(url) == "local"
    except ValueError:
        return False


def job_git_prefix(job_id: str) -> str:
    """任务在远端仓库里的临时 ref 前缀（不是分支，GitHub 页面上看不到）。"""
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        raise ValueError(f"job id {job_id!r} cannot be used in a git ref")
    return f"refs/calltable/jobs/{job_id}"


def run_git(args: list[str], *, cwd: str | Path, timeout: float
            ) -> subprocess.CompletedProcess:
    """hub 侧跑 git：不交互、不分页、没有控制终端，超时有界。

    hub 用自己进程的 git 凭据（ssh key、credential helper）。``start_new_session``
    让 ssh 打不开 /dev/tty，要密码 / 确认 host key 时直接失败而不是卡住。
    """
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
                "GCM_INTERACTIVE": "never"})
    try:
        proc = subprocess.run(
            ["git", "-c", "core.pager=cat", *args], cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            start_new_session=(os.name != "nt"))
    except subprocess.TimeoutExpired as exc:
        raise CloudRunError(f"git {args[0]} timed out after {int(timeout)}s") from exc
    except OSError as exc:
        raise CloudRunError(f"git failed to start: {exc}") from exc
    if proc.returncode != 0:
        raise CloudRunError(f"git {args[0]} exited {proc.returncode}: "
                            f"{(proc.stderr or proc.stdout).strip()[-500:]}")
    return proc


# ------------------------------------------------------------------ 仓库可见性
#: 匿名 ``ls-remote`` 的超时（秒）。
VISIBILITY_TIMEOUT = 20.0
#: 「公开 / 私有」结论的缓存时长；「无法确认」更快重试。
VISIBILITY_TTL = 600.0
VISIBILITY_UNKNOWN_TTL = 60.0
PUBLIC, PRIVATE, UNKNOWN, UNCHECKED = "public", "private", "unknown", "unchecked"
#: 匿名访问失败、且明确表示「要认证 / 匿名看不到」的报错（小写匹配）。
#: 403 不算：挡在公开仓库前面的代理 / 防火墙也会回 403，认成「私有」就会把
#: 快照推到公开仓库；它按「无法确认」处理（不推送）。
_PRIVATE_PATTERNS = re.compile(
    r"authentication failed|could not read (username|password)"
    r"|terminal prompts disabled|repository not found"
    r"|repository '[^']*' not found|returned error: (401|404)"
    r"|\bhttp (401|404)\b|http basic: access denied"
    r"|invalid username or password|requested url returned error: 40[14]")
#: 匿名检查时去掉的环境变量：任何可能带凭据或额外配置的来源。
_CREDENTIAL_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_CREDENTIAL_ENV = ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG",
                   "GIT_ASKPASS", "SSH_ASKPASS", "CALLTABLE_GIT_TOKEN",
                   "CALLTABLE_TOKEN", "GIT_DIR", "GIT_COMMON_DIR",
                   "XDG_CONFIG_HOME")


def is_http_url(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def anonymous_ls_remote(url: str, timeout: float = VISIBILITY_TIMEOUT
                        ) -> subprocess.CompletedProcess:
    """不带任何凭据地 ``git ls-remote <url> HEAD``（判断仓库是否匿名可读）。

    关掉所有凭据来源：全局 / 系统 git 配置指向空设备、``credential.helper``
    清空、``http.extraheader`` 清空、askpass 是一个必定失败的命令、不许终端
    提示，HOME 换成临时空目录（不读 ~/.netrc），也不在任何仓库里运行。
    超时 / 启动失败按原样抛出（调用方视为「无法确认」）。
    """
    env = {key: value for key, value in os.environ.items()
           if key not in _CREDENTIAL_ENV
           and not key.startswith(_CREDENTIAL_ENV_PREFIXES)}
    with tempfile.TemporaryDirectory(prefix="ct-visibility-") as home:
        env.update({
            "HOME": home,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "false",
            "SSH_ASKPASS": "false",
            "GCM_INTERACTIVE": "never",
            "GIT_PAGER": "cat",
        })
        return subprocess.run(
            ["git", "-c", "credential.helper=", "-c", "http.extraheader=",
             "-c", "core.askPass=false", "-c", "core.pager=cat",
             "ls-remote", url, "HEAD"],
            cwd=home, env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            start_new_session=(os.name != "nt"))


def repo_visibility(url: str) -> tuple[str, str]:
    """``(结论, 说明)``：public / private / unknown；非 http(s) URL 为 unchecked。"""
    if not is_http_url(url):
        return UNCHECKED, "not an http(s) URL"
    try:
        proc = anonymous_ls_remote(url, timeout=VISIBILITY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return UNKNOWN, f"anonymous ls-remote timed out after {int(VISIBILITY_TIMEOUT)}s"
    except OSError as exc:
        return UNKNOWN, f"git failed to start: {exc}"
    if proc.returncode == 0:
        return PUBLIC, "anonymous ls-remote succeeded"
    detail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
    if _PRIVATE_PATTERNS.search(detail.lower()):
        return PRIVATE, detail
    return UNKNOWN, detail or f"git ls-remote exited {proc.returncode}"


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:500]


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
        #: URL → (可见性结论, 过期时间)：避免每个任务都多一次网络调用。
        self._visibility: dict[str, tuple[str, str, float]] = {}
        #: push_url → 删除远端临时 ref 失败后，下一次允许碰这个远端的时间。
        self._git_retry_at: dict[str, float] = {}
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
    def _closing(self) -> bool:
        """hub 正在关闭：这一轮不再发起新的 gcloud 调用，剩下的留给下次启动。"""
        return self.hub._stop.is_set()

    def tick(self) -> None:
        with self._lock:
            for host, settings in self.settings.items():
                if self._closing():
                    return
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
            # 关闭检查必须在 claim 之前：claim 之后不拉起，任务会带着 pending:
            # 占位一直等到 attach 超时。
            if self._closing():
                return
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
                self.store.finish_if_status(
                    job["id"], (QUEUED,), status="error", exit_code=None,
                    error=message,
                    cleanup_at=now() + self.hub.config.retention_failure)

    def _launch(self, host: str, settings: CloudRunSettings, job: dict) -> None:
        cloud_job = settings.job_name(job["project"], job["slot"])
        vcpu, mem = settings.slot_resources(job["slot"])
        self.store.cloud_record_launch(job["id"], host, cloud_job=cloud_job,
                                       vcpu=vcpu, memory_gib=mem)
        env = {"CALLTABLE_JOB_ID": job["id"], "CALLTABLE_EXEC_HOST": host}
        if settings.hub_url:
            env["CALLTABLE_HUB_URL"] = settings.hub_url
        env.update(self._push_git_refs(job))
        try:
            execution = self.api.run_job(
                settings, cloud_job, env=env,
                task_timeout=settings.task_timeout(job.get("timeout")))
        except Exception as exc:
            note = f"cloud run launch failed ({cloud_job}): {exc}"
            self.store.cloud_update(job["id"], state="failed", ended=now(),
                                    note=note[:500])
            self._fail(job["id"], note[:1000])
            return
        self.store.cloud_update(job["id"], execution=execution, state="launched")
        # 不打印镜像：实际跑哪个镜像由 Cloud Run 上的 job 定义决定，hub 这边
        # 只能从 profile 推一个名字，未必是真正在跑的那个 tag。
        print(f"[cloudrun] {job['id']} -> {cloud_job} execution {execution}",
              flush=True)

    # ---- git 中转
    def _push_git_refs(self, job: dict) -> dict[str, str]:
        """profile 配了 ``cloudrun.git``：把任务提交（和基线）推成远端临时 ref。

        成功返回要传给 execution 的环境变量；任何失败只打一行警告并返回空，
        容器照旧从 hub 取 bundle——推送失败绝不能让任务失败。
        """
        try:
            profile = self.hub.hub_profile(job)
            relay = GitRelay.from_profile(profile.cloudrun if profile else None)
        except Exception as exc:
            print(f"[cloudrun] {job['id']}: cloudrun.git ignored: {exc}", flush=True)
            return {}
        # hub 正在关闭：不再发起慢速推送，照旧拉起（容器走 hub bundle）。
        if relay is None or self._closing():
            return {}
        # 快照含未提交的改动：仓库匿名可读（例如被改成了公开）就绝不推送；
        # 确认不了也不推（fail closed）。容器照旧走 hub bundle。
        verdict, detail = self._repo_visibility(relay.url)
        if verdict == PUBLIC:
            print(f"[cloudrun] {job['id']}: refusing to push to {relay.url}: the "
                  "repository is publicly readable; falling back to hub bundles",
                  flush=True)
            return {}
        if verdict == UNKNOWN:
            print(f"[cloudrun] {job['id']}: not pushing to {relay.url}: could not "
                  f"verify that the repository is private ({' '.join(detail.split())[:300]}); "
                  "falling back to hub bundles", flush=True)
            return {}
        if verdict == UNCHECKED and not is_local_url(relay.url):
            # 校验本应已经拒绝；这里兜底：查不了可见性的网络地址一律不推。
            print(f"[cloudrun] {job['id']}: not pushing to {relay.url}: only "
                  "http(s) URLs can be checked for public visibility; falling "
                  "back to hub bundles", flush=True)
            return {}
        if self._closing():
            return {}
        try:
            prefix = job_git_prefix(job["id"])
            sha = str(job.get("sha") or "")
            if not sha:
                raise CloudRunError("job has no commit sha")
            mirror = self.hub.mirror_dir / f"{job['project']}.git"
            if not (mirror / "HEAD").exists():
                raise CloudRunError("the hub has no mirror for this project")
            refs = {f"{prefix}/head": sha}
            base = str(job.get("base_ref_sha") or "")
            # 基线不在 hub 镜像里就只推 head：容器缺基线时照常向 hub 要。
            if base and gitops.has_commit(mirror, base):
                refs[f"{prefix}/base"] = base
            # 先记下计划推的 ref 再推：推送超时但服务端已经收下、只收下一部分、
            # 或 hub 在推完记录前退出时，正常的清理流程都能把落地的 ref 删掉。
            self.store.cloud_update(job["id"], git_refs=json.dumps({
                "project": job["project"], "push_url": relay.push_url,
                "refs": sorted(refs)}))
            run_git(["push", "--quiet", "--no-verify", relay.push_url,
                     *[f"+{value}:{ref}" for ref, value in refs.items()]],
                    cwd=mirror, timeout=GIT_PUSH_TIMEOUT)
        except Exception as exc:
            # 记录保留：清理时把可能已经落地的 ref 删掉（一个都没有就算完成）。
            print(f"[cloudrun] {job['id']}: git push to {relay.push_url} failed, "
                  f"falling back to hub bundles: {_one_line(exc)}", flush=True)
            return {}
        return {"CALLTABLE_GIT_URL": relay.url, "CALLTABLE_GIT_REFS": prefix}

    def _repo_visibility(self, url: str) -> tuple[str, str]:
        """带缓存的 :func:`repo_visibility`（公开 / 私有 10 分钟，无法确认 1 分钟）。"""
        cached = self._visibility.get(url)
        current = now()
        if cached is not None and current < cached[2]:
            return cached[0], cached[1]
        verdict, detail = repo_visibility(url)
        if verdict != UNCHECKED:
            ttl = VISIBILITY_UNKNOWN_TTL if verdict == UNKNOWN else VISIBILITY_TTL
            self._visibility[url] = (verdict, detail, current + ttl)
        return verdict, detail

    def _cleanup_git_refs(self, host: str) -> None:
        """execution 记录关闭后删掉推过的临时 ref；失败的留到以后的对账再试。

        退避按 push_url：一个远端删除失败后，它的其余记录在重试时间前都跳过；
        每轮最多尝试 ``GIT_CLEANUP_BATCH`` 条，不让清理长时间占住线程。
        """
        attempts = 0
        for row in self.store.cloud_git_pending(host):
            if self._closing() or attempts >= GIT_CLEANUP_BATCH:
                return
            job_id = row["job_id"]
            try:
                info = json.loads(row["git_refs"])
                refs = [str(ref) for ref in info.get("refs") or []]
                push_url = str(info["push_url"])
                project = str(info["project"])
            except (ValueError, KeyError, TypeError, AttributeError):
                print(f"[cloudrun] {job_id}: unreadable git_refs record dropped",
                      flush=True)
                self.store.cloud_update(job_id, git_refs="")
                continue
            if now() < self._git_retry_at.get(push_url, 0.0):
                continue
            attempts += 1
            try:
                self._delete_remote_refs(project, push_url, refs)
            except Exception as exc:
                self._git_retry_at[push_url] = now() + GIT_CLEANUP_RETRY
                print(f"[cloudrun] {job_id}: deleting git refs on {push_url} "
                      f"failed (will retry): {_one_line(exc)}", flush=True)
                continue
            self._git_retry_at.pop(push_url, None)
            self.store.cloud_update(job_id, git_refs="")

    def _delete_remote_refs(self, project: str, push_url: str,
                            refs: list[str]) -> None:
        if not refs:
            return
        # git push 需要在某个仓库里跑；用 hub 的项目镜像（没有就临时建一个空的）。
        mirror = self.hub.mirror_for(project)
        try:
            run_git(["push", "--quiet", "--no-verify", push_url,
                     *[f":{ref}" for ref in refs]],
                    cwd=mirror, timeout=GIT_DELETE_TIMEOUT)
            return
        except CloudRunError:
            # 部分 ref 已经不在（手动删过、上次只删掉一半）时 push 也报错：
            # 远端已经一个都不剩就算删干净了。
            listed = run_git(["ls-remote", push_url, *refs], cwd=mirror,
                             timeout=GIT_LS_REMOTE_TIMEOUT).stdout
            remaining = {line.split()[-1] for line in listed.splitlines()
                         if line.strip()}
            if remaining & set(refs):
                raise

    def _reconcile(self, host: str, settings: CloudRunSettings) -> None:
        try:
            self._reconcile_executions(host, settings)
        finally:
            # 放在最后：这一轮刚关掉的记录也能马上清理远端 ref。
            if not self._closing():
                try:
                    self._cleanup_git_refs(host)
                except Exception as exc:  # pragma: no cover - 兜底
                    print(f"[cloudrun] git cleanup {host}: {type(exc).__name__}: "
                          f"{exc}", flush=True)

    def _reconcile_executions(self, host: str, settings: CloudRunSettings) -> None:
        current = now()
        describe = (current - self._last_reconcile.get(host, 0.0)
                    >= settings.reconcile_interval)
        if describe:
            self._last_reconcile[host] = current
        rows = [row for row in self.store.cloud_executions(active_only=True)
                if row["host"] == host]
        for row in rows:
            if self._closing():
                return
            job = self.store.job(row["job_id"])
            job_done = job is None or job.get("status") in TERMINAL
            execution = row["execution"]
            if not execution:
                if job_done:
                    self.store.cloud_update(row["job_id"], state="ended",
                                            ended=current)
                    continue
                # execution 名没记下来（hub 在 gcloud execute 期间或之前退出）：
                # 无从取消，但 Cloud Run 那边可能已经拉起，容器照样能 attach 并
                # 跑完；只按同样的 attach / 心跳超时回收，并关掉这条记录止住花费。
                timeout = self._timed_out(row, settings, current)
                if timeout:
                    if timeout == "attach":
                        reason = (f"no executor attached within "
                                  f"{settings.attach_timeout}s")
                    else:
                        seen = row["last_seen"] or row["attached"]
                        reason = (f"executor heartbeat lost for "
                                  f"{int(current - seen)}s")
                    self.store.cloud_update(row["job_id"], state="failed",
                                            ended=current, note=reason[:500])
                    self._fail(row["job_id"], "cloud run execution name was never "
                                              f"recorded (hub restarted?): {reason}")
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
                    self._fail(job["id"], f"cloud run execution {execution} ended "
                                          f"before the job reported ({detail})")
                continue
            if job_done:
                finished_at = float(job.get("finished") or current) if job else current
                if current - finished_at >= settings.timeout_grace:
                    self._cancel(row, settings, "job already finished")
                continue
            timeout = self._timed_out(row, settings, current)
            if timeout == "attach":
                self._cancel(row, settings, "no executor attached within "
                             f"{settings.attach_timeout}s")
                self._fail(job["id"], f"cloud run execution {execution} never "
                                      f"attached within {settings.attach_timeout}s")
            elif timeout == "heartbeat":
                seen = row["last_seen"] or row["attached"]
                self._cancel(row, settings, "executor heartbeat lost")
                self._fail(job["id"], f"cloud run execution {execution}: executor "
                                      f"heartbeat lost for {int(current - seen)}s")
        # claim 之后、写 cloud_executions 之前 hub 退出：任务停在 running +
        # pending: 占位，没有 execution 也不会有人 attach。按 attach_timeout
        # （从任务开始 running 算）回收，否则它永远占着 slot。
        tracked = {row["job_id"] for row in rows}
        for job in self.store.jobs(status=RUNNING, limit=10000):
            if (job.get("host") != host or job["id"] in tracked
                    or not (job.get("worker") or "").startswith(PENDING_WORKER_PREFIX)):
                continue
            started = float(job.get("started") or current)
            if current - started >= settings.attach_timeout:
                self._fail(job["id"], "cloud run launch was never recorded "
                                      "(hub restarted?): no executor attached "
                                      f"within {settings.attach_timeout}s")

    @staticmethod
    def _timed_out(row: dict, settings: CloudRunSettings,
                   current: float) -> str:
        """``"attach"`` / ``"heartbeat"``：哪种超时；没超时返回空串。"""
        attached = row["attached"]
        if not attached and current - row["launched"] >= settings.attach_timeout:
            return "attach"
        seen = row["last_seen"] or attached
        if attached and seen and current - seen >= settings.heartbeat_timeout:
            return "heartbeat"
        return ""

    def _cancel(self, row: dict, settings: CloudRunSettings, reason: str) -> None:
        try:
            self.api.cancel_execution(settings, row["execution"])
        except Exception as exc:
            print(f"[cloudrun] cancel {row['execution']}: {exc}", flush=True)
        self.store.cloud_update(row["job_id"], state="cancelled", ended=now(),
                                note=reason[:500])

    def _fail(self, job_id: str, message: str) -> None:
        # 「还在 running 吗」与写 error 在 store 的同一把锁里完成：慢速 gcloud
        # 调用期间执行器上报的终态不会被这里覆盖。
        failed = self.store.finish_if_status(
            job_id, (RUNNING,), status="error", exit_code=None, error=message,
            cleanup_at=now() + self.hub.config.retention_failure)
        if failed is not None:
            print(f"[cloudrun] {job_id}: {message}", flush=True)
