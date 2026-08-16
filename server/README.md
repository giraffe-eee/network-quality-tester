# 网络质量检测器服务端

这是网络质量检测器的 Linux 测试端点。它不依赖第三方 Python 包，负责创建测试会话、回显 UDP 游戏包和压力包，并提供 TCP 回显。客户端会根据这些回包计算丢包、抖动、尾延迟、压力吞吐和综合评分。

## 部署前准备

- Ubuntu 22.04 或更新版本的 Linux 服务器
- 一条可从客户端访问的公网 IPv4
- root 或 sudo 权限
- 云厂商安全组可以放行三个端口

服务端需要开放以下端口：

| 端口 | 协议 | 用途 |
| ---: | --- | --- |
| 37820 | TCP | 创建会话、结束会话和健康检查 |
| 37821 | UDP | 游戏实时包与 UDP 压力回显 |
| 37822 | TCP | TCP 可靠回显 |

三个端口必须同时放行。只开放 TCP 端口会导致 UDP 指标失败，只开放 UDP 端口会导致 TCP 指标失败。

## 安装服务

以下命令以 root 用户为例。普通用户请在需要的位置加 `sudo`。

### 1. 安装基础工具

```bash
apt-get update
apt-get install -y git python3
```

### 2. 下载项目并安装

```bash
git clone https://github.com/giraffe-eee/network-quality-tester.git /opt/network-quality-tester-src
cd /opt/network-quality-tester-src/server
bash install.sh
```

安装脚本会完成以下工作：

- 创建没有登录权限的 `nqtester` 服务用户
- 将服务端安装到 `/opt/network-quality-tester`
- 创建 `/etc/network-quality-tester.conf`
- 注册并立即启动 `network-quality.service`
- 如果 UFW 已启用，自动放行三个服务端口

服务端只使用 Python 标准库，不需要执行 `pip install`。

## 配置端口

安装脚本生成的配置文件默认如下：

```ini
NQ_HOST=0.0.0.0
NQ_CONTROL_PORT=37820
NQ_UDP_PORT=37821
NQ_TCP_PORT=37822
NQ_LOG_LEVEL=INFO
```

通常不需要修改。客户端当前按这三个默认端口连接；如果修改端口，需要同时调整客户端连接配置。

修改配置后重启服务：

```bash
systemctl restart network-quality.service
```

## 云安全组和防火墙

在云厂商控制台的入方向规则中加入：

| 来源 | 端口 | 协议 |
| --- | ---: | --- |
| 需要测试的客户端地址，或 `0.0.0.0/0` | 37820 | TCP |
| 需要测试的客户端地址，或 `0.0.0.0/0` | 37821 | UDP |
| 需要测试的客户端地址，或 `0.0.0.0/0` | 37822 | TCP |

如果只给自己使用，建议把来源限制为自己的公网地址；如果要让不同网络的用户都能测试，才使用全网来源规则。

服务器启用了 UFW 时，也可以手动检查规则：

```bash
ufw status
ufw allow 37820/tcp
ufw allow 37821/udp
ufw allow 37822/tcp
```

不要只配置 HTTP/HTTPS 反向代理。这个服务需要原生 UDP 和 TCP 端口，普通 Web 代理不能替代它。

## 检查服务状态

查看服务状态和实时日志：

```bash
systemctl status network-quality.service
journalctl -u network-quality.service -f
```

确认三个监听端口：

```bash
ss -lntup | grep -E '37820|37821|37822'
```

在服务器本机执行健康检查：

```bash
python3 - <<'PY'
import socket

with socket.create_connection(("127.0.0.1", 37820), timeout=5) as sock:
    sock.sendall(b'{"op":"health"}\n')
    print(sock.recv(4096).decode())
PY
```

返回内容中应包含 `"ok":true`。如果本机检查正常，但客户端连接超时，优先检查云安全组、公网 IPv4 和服务器防火墙。

## 客户端连接

1. 打开客户端。
2. 在“测试节点”中选择“自定义节点”。
3. 填写服务器公网 IPv4 或可解析到 IPv4 的域名。
4. 不要填写 `http://`、`https://` 或端口号。
5. 选择测试时长并开始检测。

客户端地址栏只需要主机地址，端口由程序按服务端默认配置连接。

## 更新服务端

在最初下载项目的目录执行：

```bash
cd /opt/network-quality-tester-src
git pull --ff-only
cd server
bash install.sh
```

安装脚本会覆盖服务程序和 systemd 单元，但不会覆盖已经存在的 `/etc/network-quality-tester.conf`。更新后可用下面的命令确认服务已重新启动：

```bash
systemctl status network-quality.service
```

## 常见问题

### 控制连接失败

检查服务是否运行，并确认 TCP `37820` 同时在云安全组和 UFW 中放行。

### UDP 丢包显示异常或 UDP 指标为空

确认 `37821/udp` 已放行。安全组中的 TCP 规则不会放行 UDP。

### TCP 指标为空

确认 `37822/tcp` 已放行，并查看服务日志中是否有连接错误。

### 服务启动失败

先查看完整日志：

```bash
journalctl -u network-quality.service -n 100 --no-pager
```

确认配置文件中的端口没有被其他程序占用：

```bash
ss -lntup | grep -E '37820|37821|37822'
```

## 安全建议

服务端面向网络提供测试接口，没有账号登录流程。自用时应在云安全组中限制来源地址；如果必须对公网开放，请只开放这里列出的端口，并定期查看 systemd 日志。服务端会限制单个来源的并发会话和单次测试时长，测试数据只保存在运行内存中。
