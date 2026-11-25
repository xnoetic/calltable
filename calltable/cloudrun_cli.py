"""``ct cloudrun ...``：Cloud Run 主机的镜像构建、job 定义与状态。

这些命令在 **hub 所在机器**上用 hub 的配置运行（``--config``），因为 Cloud Run
设置在 ``hosts.<name>.cloudrun``，项目镜像在 profile 的 ``cloudrun`` 段。

默认只打印将要执行的 gcloud / docker 命令；加 ``--apply`` 才真正执行。
镜像构建（Cloud Build / docker push）和 job 定义本身都可能产生费用或改动云端
资源，所以必须显式 ``--apply``。
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .cloudrun import (PROVIDER, CloudRunSettings, project_image,
                       sanitize_name)
from .config import Config
from .profiles import find_profile, load_profiles

ASSETS = Path(__file__).resolve().parent / "cloudrun_assets"
PACKAGE = Path(__file__).resolve().parent


class UsageError(RuntimeError):
    pass


# ------------------------------------------------------------------ 解析
def cloudrun_settings(config: Config, host: str | None = None) -> CloudRunSettings:
    hosts = {name: spec for name, spec in config.hosts.items()
             if spec.provider == PROVIDER}
    if not hosts:
        raise UsageError("no host with provider \"cloudrun\" in this config")
    if host is None:
        if len(hosts) > 1:
            raise UsageError("several cloudrun hosts; pass --host "
                             f"({', '.join(sorted(hosts))})")
        host = next(iter(hosts))
    if host not in hosts:
        raise UsageError(f"{host} is not a cloudrun host "
                         f"(cloudrun hosts: {', '.join(sorted(hosts))})")
    return CloudRunSettings.from_host(host, hosts[host].provider_config,
                                      hub_url=config.public_url)


def profile_cloudrun(config: Config, project: str, host: str) -> dict:
    profile = find_profile(load_profiles(config.profiles_dir), project, host)
    return dict(profile.cloudrun) if profile is not None else {}


def host_slots(config: Config, host: str) -> list[str]:
    spec = config.hosts[host]
    return [slot for slot, limit in spec.slots.items() if int(limit) > 0]


# ------------------------------------------------------------------ 镜像
def render_dockerfile(cloudrun: dict) -> str:
    template = (ASSETS / "Dockerfile.template").read_text(encoding="utf-8")
    base = str(cloudrun.get("base_image") or "python:3.12-slim").strip()
    template = template.replace("ARG BASE_IMAGE=python:3.12-slim",
                                f"ARG BASE_IMAGE={base}", 1)
    install = cloudrun.get("install") or []
    if isinstance(install, str):
        install = [install]
    lines = [f"RUN {step}" for step in (str(x).strip() for x in install) if step]
    template = template.replace("# @@INSTALL@@",
                                "\n".join(lines) if lines
                                else "# (no profile install steps)", 1)
    env = cloudrun.get("env") or {}
    env_lines = [f"ENV {key}={shlex.quote(str(value))}"
                 for key, value in sorted(env.items())]
    template = template.replace("# @@ENV@@",
                                "\n".join(env_lines) if env_lines
                                else "# (no profile image env)", 1)
    return template


def write_build_context(cloudrun: dict, dest: Path) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "Dockerfile").write_text(render_dockerfile(cloudrun), encoding="utf-8")
    entry = dest / "entrypoint.sh"
    shutil.copyfile(ASSETS / "entrypoint.sh", entry)
    entry.chmod(0o755)
    target = dest / "calltable"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(PACKAGE, target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dest / ".dockerignore").write_text("**/__pycache__\n", encoding="utf-8")
    return dest


def image_commands(settings: CloudRunSettings, image: str, context: Path,
                   builder: str) -> list[list[str]]:
    if builder == "cloudbuild":
        return [["gcloud", "builds", "submit", str(context),
                 f"--project={settings.project}", f"--region={settings.region}",
                 f"--tag={image}"]]
    if builder == "docker":
        return [["docker", "build", "--platform=linux/amd64", "-t", image,
                 str(context)],
                ["docker", "push", image]]
    raise UsageError(f"unknown builder {builder!r} (cloudbuild or docker)")


def image_name(settings: CloudRunSettings, project: str, cloudrun: dict,
               tag: str | None) -> str:
    if tag:
        return f"{settings.registry}/{sanitize_name(project)}:{tag}"
    return project_image(settings, project, cloudrun)


# ------------------------------------------------------------------ job 定义
def job_commands(settings: CloudRunSettings, project: str, image: str,
                 slots: list[str]) -> list[list[str]]:
    commands = []
    for slot in slots:
        spec = settings.resources.get(slot) or {}
        cpu = spec.get("cpu", 2)
        memory = spec.get("memory", "8Gi")
        env = [f"CALLTABLE_EXEC_HOST={settings.host}"]
        if settings.hub_url:
            env.append(f"CALLTABLE_HUB_URL={settings.hub_url}")
        secrets = [f"CALLTABLE_TOKEN={settings.hub_token_secret}:latest"]
        if settings.tailscale_authkey_secret:
            secrets.append(f"TS_AUTHKEY={settings.tailscale_authkey_secret}:latest")
        cmd = ["gcloud", "run", "jobs", "deploy",
               settings.job_name(project, slot),
               f"--project={settings.project}", f"--region={settings.region}",
               f"--image={image}", f"--cpu={cpu}", f"--memory={memory}",
               "--tasks=1", "--parallelism=1", "--max-retries=0",
               f"--task-timeout={settings.max_task_timeout}s",
               "--set-secrets=" + ",".join(secrets),
               "--labels=" + ",".join([
                   "managed-by=calltable",
                   f"calltable-host={sanitize_name(settings.host)}",
                   f"calltable-project={sanitize_name(project)}",
                   f"calltable-slot={sanitize_name(slot)}"])]
        if settings.service_account:
            cmd.append(f"--service-account={settings.service_account}")
        disk = str(spec.get("disk") or "").strip()
        if disk:
            env.append("CALLTABLE_WORK_DIR=/work")
            cmd += [f"--add-volume=name=work,type=ephemeral-disk,size={disk}",
                    "--add-volume-mount=volume=work,mount-path=/work"]
        cmd.append("--set-env-vars=" + ",".join(env))
        commands.append(cmd)
    return commands


# ------------------------------------------------------------------ 一次性准备
def bootstrap_commands(settings: CloudRunSettings,
                       hub_account: str = "") -> list[list[str]]:
    project = f"--project={settings.project}"
    repository = settings.registry.rsplit("/", 1)[-1]
    commands = [
        ["gcloud", "services", "enable", "run.googleapis.com",
         "artifactregistry.googleapis.com", "secretmanager.googleapis.com",
         "cloudbuild.googleapis.com", project],
        ["gcloud", "artifacts", "repositories", "create", repository,
         "--repository-format=docker", f"--location={settings.region}",
         "--description=calltable Cloud Run executor images", project],
        ["gcloud", "secrets", "create", settings.hub_token_secret,
         "--replication-policy=automatic", project],
    ]
    if settings.tailscale_authkey_secret:
        commands.append(["gcloud", "secrets", "create",
                         settings.tailscale_authkey_secret,
                         "--replication-policy=automatic", project])
    if settings.service_account:
        member = f"serviceAccount:{settings.service_account}"
        for secret in (settings.hub_token_secret, settings.tailscale_authkey_secret):
            if secret:
                commands.append(["gcloud", "secrets", "add-iam-policy-binding",
                                 secret, f"--member={member}",
                                 "--role=roles/secretmanager.secretAccessor",
                                 project])
    if hub_account:
        member = (hub_account if ":" in hub_account
                  else f"serviceAccount:{hub_account}")
        commands.append(["gcloud", "projects", "add-iam-policy-binding",
                         settings.project, f"--member={member}",
                         "--role=roles/run.developer", "--condition=None"])
        if settings.service_account:
            commands.append(["gcloud", "iam", "service-accounts",
                             "add-iam-policy-binding", settings.service_account,
                             f"--member={member}",
                             "--role=roles/iam.serviceAccountUser", project])
    return commands


# ------------------------------------------------------------------ 执行
def print_commands(commands: list[list[str]]) -> None:
    for cmd in commands:
        print("  " + shlex.join(cmd))


def apply_commands(commands: list[list[str]]) -> int:
    for cmd in commands:
        print("+ " + shlex.join(cmd), flush=True)
        code = subprocess.call(cmd)
        if code != 0:
            print(f"command failed with exit code {code}", file=sys.stderr)
            return code
    return 0


def cmd_cloudrun(config: Config, args) -> int:
    action = args.cloudrun_action
    try:
        if action == "status":
            from .cli import _client
            data = _client(config, args).get_json("/api/cloudrun") or {}
            hosts = data.get("hosts") or {}
            if not hosts:
                print("no cloudrun hosts on this hub")
                return 0
            for name, info in sorted(hosts.items()):
                print(f"{name}: project={info['project']} region={info['region']} "
                      f"spend=${info['month_spend_usd']:.2f} / "
                      f"${info['monthly_budget_usd']:.2f} this month"
                      f"{' (OVER BUDGET)' if info.get('over_budget') else ''} "
                      f"active={info['active_executions']}")
                for row in info.get("executions") or []:
                    print(f"  {row['job_id']}  {row['cloud_job']}  "
                          f"{row['execution'] or '-'}  {row['state']}")
            return 0

        settings = cloudrun_settings(config, getattr(args, "host", None))
        if action == "bootstrap":
            commands = bootstrap_commands(settings, args.hub_account or "")
            print("one-time setup for cloudrun host "
                  f"{settings.host} (project {settings.project}):")
            print_commands(commands)
            print("then store the secret values (from your own shell):\n"
                  f"  printf %s \"$CALLTABLE_TOKEN\" | gcloud secrets versions add "
                  f"{settings.hub_token_secret} --data-file=- "
                  f"--project={settings.project}\n"
                  f"  printf %s \"$TS_AUTHKEY\" | gcloud secrets versions add "
                  f"{settings.tailscale_authkey_secret} --data-file=- "
                  f"--project={settings.project}")
            return apply_commands(commands) if args.apply else 0

        project = args.project
        cloudrun = profile_cloudrun(config, project, settings.host)
        if action == "image":
            image = image_name(settings, project, cloudrun, args.tag)
            if args.context:
                context = Path(args.context)
            else:
                context = Path(tempfile.mkdtemp(prefix=f"ct-cloudrun-{project}-"))
            write_build_context(cloudrun, context)
            commands = image_commands(settings, image, context, args.builder)
            print(f"build context: {context}")
            print(f"image: {image}")
            print_commands(commands)
            if not args.apply:
                print("(dry run; add --apply to build and push)")
                return 0
            return apply_commands(commands)

        if action == "jobs":
            image = image_name(settings, project, cloudrun, args.tag)
            slots = ([s.strip() for s in args.slots.split(",") if s.strip()]
                     if args.slots else host_slots(config, settings.host))
            unknown = [s for s in slots if s not in settings.resources]
            if unknown:
                raise UsageError(f"no resources configured for slot(s) "
                                 f"{', '.join(unknown)}")
            commands = job_commands(settings, project, image, slots)
            print(f"Cloud Run job definitions for {project} on {settings.host}:")
            print_commands(commands)
            if not args.apply:
                print("(dry run; add --apply to create/update the job definitions)")
                return 0
            return apply_commands(commands)
    except UsageError as exc:
        print(f"cloudrun: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"cloudrun: {exc}", file=sys.stderr)
        return 2
    print(f"unknown cloudrun action {action!r}", file=sys.stderr)
    return 2


def add_parser(csub, add_cli_options) -> None:
    parser = csub.add_parser(
        "cloudrun", help="Cloud Run host: images, job definitions, status")
    sub = parser.add_subparsers(dest="cloudrun_action", required=True)

    status = sub.add_parser("status", help="month-to-date spend and executions")
    add_cli_options(status)

    boot = sub.add_parser("bootstrap", help="print one-time GCP setup commands")
    add_cli_options(boot)
    boot.add_argument("--host")
    boot.add_argument("--hub-account", default="",
                      help="identity the hub uses for gcloud (service account "
                           "email or user:you@example.com)")
    boot.add_argument("--apply", action="store_true")

    image = sub.add_parser("image", help="build and push a project's executor image")
    add_cli_options(image)
    image.add_argument("--host")
    image.add_argument("--project", required=True)
    image.add_argument("--tag", help="image tag (default: profile image or :latest)")
    image.add_argument("--builder", choices=["cloudbuild", "docker"],
                       default="cloudbuild")
    image.add_argument("--context", help="write the build context here")
    image.add_argument("--apply", action="store_true")

    jobs = sub.add_parser("jobs", help="create/update a project's Cloud Run jobs")
    add_cli_options(jobs)
    jobs.add_argument("--host")
    jobs.add_argument("--project", required=True)
    jobs.add_argument("--tag")
    jobs.add_argument("--slots", help="comma separated (default: the host's slots)")
    jobs.add_argument("--apply", action="store_true")

    for sp in (status, boot, image, jobs):
        sp.set_defaults(func=cmd_cloudrun)
