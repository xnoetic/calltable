# 懒猫微服看板 app

把 calltable hub 的看板装成懒猫微服里的一个应用，打开 `https://calltable.<盒子名>.heiyu.space` 就能看，不需要 Tailscale，也不用在链接里带 token。原来的 `http://<hub>:<port>/` 看板照常可用，两者看的是同一个 hub。

## 工作方式

- 应用没有容器：懒猫的 `lzcinit` 把所有请求转给 hub（`application.upstreams`）。
- 转发前，`injects` 里的脚本做两件事：
  1. 只放行盒子管理员（`X-HC-User-Role` 为 `ADMIN`，这个请求头由懒猫网关注入，浏览器伪造不了），其他人返回 403；`package.yml` 里的 `admin_only: true` 让普通用户在启动器里看不到这个应用。
  2. 丢掉浏览器带来的 `Authorization`，换成 `Bearer <hub token>`。
- token 是安装参数（`lzc-deploy-params.yml` 里的 `type: secret`），在盒子上安装或重新配置时填写，不进 lpk，也不会发给浏览器。看板页面本身不持有 token：它的请求不带 token，由这个应用在服务端补上。

## hub 这边

hub 要在盒子连得到的地址上也监听。在 LightOS 虚拟机里，这个地址是虚拟机朝向盒子的网卡（例如 `lzc-<实例名>` 网卡上的地址，下面以 `100.64.0.10` 为例）。在 hub 配置里加上：

```json
"hub_extra_binds": ["100.64.0.10"]
```

然后重启 hub（在跑的任务不受影响，可以用 `deploy/drain-restart.sh`）。`hub_bind` 不变，原来的看板地址照旧。新监听和原来的一样，`/api/*` 都要 token。

## 打包和安装

前提：盒子已开启 SSHD，并且装好、运行着「懒猫开发者工具」。然后在本机执行：

```bash
lzc-cli box add-by-ssh root <盒子局域网 IP>
```

```bash
lzc-cli project release .
```

```bash
lzc-cli lpk install io.github.xnoetic.calltable.dashboard-v0.1.0.lpk
```

装好后在盒子上打开这个应用的设置，填写：

| 参数 | 说明 |
|---|---|
| `hub_url` | 盒子连得到的 hub 地址，默认 `http://100.64.0.10:8092` |
| `hub_token` | hub 配置里的 `token` |

## 排查

- 打开后显示 403：当前登录的不是盒子管理员。
- 显示 503「还没有配置 hub token」：去应用设置里填 `hub_token`。
- 页面能打开，但提示需要 hub token：token 填错了（hub 返回 401）。
- 页面打不开或者报网关错误：盒子连不到 `hub_url`。先在虚拟机上确认 `ss -ltn` 里有 `100.64.0.10:8092`，再换成盒子局域网上转发过来的地址试试。
