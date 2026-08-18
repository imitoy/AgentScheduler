"""电脑基类与实现 (Computer) — 每个角色一台个人电脑.

标准接口 (Computer 基类), 供 LLM 工具调用:
  - power_on():  开机
  - power_off(): 关机
  - run_command(cmd): 运行命令
  - run_mcp_tool(tool_name, args): 运行 MCP 工具
  - read_file / write_file / list_dir: 个人目录文件操作

三种实现:
  - PodmanComputer: 用 podman 容器模拟虚拟电脑 (默认).
    容器名 maf-<role_id>, 工作目录 /home/agent. 本机无 podman 命令时
    自动降级为 LocalComputer (本地目录模拟, 语义一致, 便于无 podman 环境).
  - SSHComputer:   通过 ssh 连接远程主机执行命令 (需 host/user 配置).
  - LocalComputer: 本地目录模拟 (开发/降级用), 目录 data/computers/<role_id>/.

角色添加时自动创建电脑 (默认 podman): AgentRole.computer 惰性创建,
create_computer() 工厂按角色 computer_kind 选择实现.

用法:
    from src.core.computer import create_computer
    comp = create_computer("podman", role_id="CEO")
    comp.power_on()
    comp.run_command("ls -la")
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 默认 podman 镜像 (node:22-alpine: 轻量且带 node/npx, MCP 服务器在容器内跑)
DEFAULT_IMAGE = "node:22-alpine"

# MCP filesystem 服务器包 (容器内全局安装, 避免每次 npx 拉包)
MCP_FILESYSTEM_PACKAGE = "@modelcontextprotocol/server-filesystem"


class Computer(ABC):
    """电脑标准接口 (抽象基类).

    参数:
        role_id: 所属角色标识 (用于命名容器/目录隔离).

    子类需实现: power_on / power_off / run_command / run_mcp_tool /
    read_file / write_file / list_dir.
    """

    def __init__(self, role_id: str, auto_mcp: bool = False):
        self.role_id = role_id
        self._on = False
        self._auto_mcp = auto_mcp          # 自动创建的电脑: 创建时自动安装 MCP 服务器
        self._mcp_tools: dict[str, Any] = {}  # 已安装到本电脑的 MCP 工具 (name → ToolDef)
        self._mcp_server: Any = None       # 本电脑独立的 MCP 服务器连接 (懒创建)
        self._connect_error: Optional[str] = None  # 最近一次 MCP 连接失败原因 (诊断用)

    # ── MCP 会话存活检测与重连 ───────────────────────────

    def _mcp_server_alive(self) -> bool:
        """MCP 服务器会话是否存活.

        返回 False = 会话已死 (如 podman stop 杀掉 stdio 管道), 需要重建.
        无服务器实例视为存活 (无需重连). 探测走一次轻量 list_tools 往返,
        因为会话对象在进程死后可能仍在 (仅查 _session 不可靠).
        """
        srv = self._mcp_server
        if srv is None:
            return True
        probe = getattr(srv, "is_alive", None)
        if probe is not None:
            try:
                return bool(probe())
            except Exception:
                return False
        return getattr(srv, "_session", None) is not None

    def _reconnect_mcp_server(self) -> None:
        """MCP 服务器会话失效时重建 (跨天关机后 podman stop 杀死 stdio 管道).

        旧服务器对象持有死管道, 幂等短路 (install_mcp_server 见 _mcp_server
        非 None 就返回) 会让第 2 天起所有 MCP 文件工具永久失败 — 必须先
        close + 置 None + 清空旧工具表, 再重新安装.
        """
        if self._mcp_server is None or self._mcp_server_alive():
            return
        logger.warning("电脑[%s] MCP 服务器会话已失效, 正在重建...", self.role_id)
        try:
            self._mcp_server.close()
        except Exception:
            pass
        self._mcp_server = None
        self._mcp_tools.clear()  # 旧 handler 绑定的是已死服务器
        try:
            self.install_mcp_server()
        except Exception:
            logger.exception("电脑[%s] MCP 服务器重建失败", self.role_id)

    # ── 抽象接口 (子类实现) ──────────────────────────────

    @abstractmethod
    def power_on(self) -> str:
        """开机. 返回状态说明."""

    @abstractmethod
    def power_off(self) -> str:
        """关机. 返回状态说明."""

    @abstractmethod
    def run_command(self, command: str) -> str:
        """运行命令 (在个人电脑上执行). 返回命令输出."""

    @abstractmethod
    def read_file(self, path: str) -> str:
        """读取个人电脑上的文件内容."""

    @abstractmethod
    def write_file(self, path: str, content: str) -> str:
        """写入个人电脑上的文件 (自动创建父目录). 返回路径."""

    @abstractmethod
    def list_dir(self, path: str = "") -> str:
        """列出个人电脑指定目录内容 (默认工作目录)."""

    @abstractmethod
    def delete_file(self, path: str) -> str:
        """删除个人电脑上的文件. 返回状态说明 (成功/错误)."""

    # ── MCP 工具安装与执行 (所有实现共用) ────────────────

    @property
    def host_dir(self) -> str:
        """宿主机上该电脑工作目录的映射路径 (MCP 服务器授权目录).

        - LocalComputer: 就是 workdir (data/computers/<role>)
        - PodmanComputer: 容器挂载的宿主机目录 (容器内 /home/agent ↔ 宿主机 data/computers/<role>)
        - SSHComputer: 远程电脑无宿主机映射 → None (不自动装 MCP 服务器)

        MCP filesystem 服务器跑在宿主机, 授权这个目录 = 操作该角色电脑上的文件.
        """
        return str(getattr(self, "_host_dir", None) or "")

    def install_mcp_server(self) -> list[str]:
        """在本电脑上安装独立的 MCP 服务器 (filesystem, 授权本电脑目录).

        每个电脑一个独立服务器进程 (npx 启动), 授权目录 = 本电脑 host_dir,
        工具注册进 self._mcp_tools, handler 绑定本电脑自己的服务器连接 —
        执行即发生在该角色电脑的目录上. 幂等: 已安装则直接返回.

        返回:
            已安装的工具名列表.
        """
        if self._mcp_server is not None:
            return self.list_installed_mcp_tools()
        if not self._auto_mcp:
            logger.info("电脑[%s] 非自动创建, 不自动安装 MCP 服务器", self.role_id)
            return []
        if not self.host_dir:
            logger.warning("电脑[%s] 无宿主机目录映射, 跳过 MCP 服务器安装 (SSH 远程电脑)",
                           self.role_id)
            return []

        try:
            from src.python_tools.mcp_toolkit import MCPServer
            self._mcp_server = MCPServer(
                package="@modelcontextprotocol/server-filesystem",
                args=[self.host_dir],
            )
            self._mcp_server.connect()
            tools = self._mcp_server.list_tools()
            from src.core.tools import ToolDef
            for tool in tools:
                tname = getattr(tool, "name", "")
                if not tname:
                    continue
                server = self._mcp_server

                def _make_handler(srv=server, tn=tname):
                    def handler(args: dict[str, Any]) -> str:
                        return srv.call_tool(tn, args)
                    return handler

                self._mcp_tools[tname] = ToolDef(
                    name=tname,
                    description=getattr(tool, "description", "") or "",
                    input_schema=getattr(tool, "input_schema", {}) or {},
                    handler=_make_handler(),
                    source=f"mcp:{server.package} (本电脑)",
                )
            logger.info("电脑[%s] 独立 MCP 服务器已安装, %d 个工具: %s",
                        self.role_id, len(self._mcp_tools),
                        self.list_installed_mcp_tools())
        except Exception as exc:
            # 记录连接失败原因供诊断 (install_mcp_server 幂等短路,
            # 失败后 _mcp_server 仍为 None, 下次调用可重试)
            self._connect_error = str(exc)
            logger.exception("电脑[%s] MCP 服务器安装失败", self.role_id)
            return []
        return self.list_installed_mcp_tools()

    def uninstall_mcp_tool(self, tool_name: str) -> bool:
        """从本电脑卸载一个 MCP 工具. 返回是否卸载成功."""
        return self._mcp_tools.pop(tool_name, None) is not None

    def list_installed_mcp_tools(self) -> list[str]:
        """列出本电脑已安装的 MCP 工具名 (排序)."""
        return sorted(self._mcp_tools)

    def run_mcp_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """运行 MCP 工具 (在本电脑上执行).

        只执行已安装到本电脑的工具; 未安装则报错并提示可安装的工具来源.

        参数:
            tool_name: MCP 工具名.
            args:      工具参数.

        返回:
            工具执行结果文本.
        """
        td = self._mcp_tools.get(tool_name)
        if td is None:
            return (f"错误: MCP 工具 '{tool_name}' 未安装到本电脑. "
                    f"已安装: {self.list_installed_mcp_tools() or '(无)'}. "
                    f"可用 mcp_search / mcp_list 查看可用工具, 用 mcp_add 安装.")
        if td.handler is None:
            return f"错误: 工具 '{tool_name}' 缺少可执行 handler."
        try:
            return str(td.handler(args))
        except Exception as exc:
            logger.exception("MCP 工具 %s 执行失败", tool_name)
            return f"错误: 工具 '{tool_name}' 执行失败 - {exc}"

    # ── 通用 ──────────────────────────────────────────────

    @property
    def is_on(self) -> bool:
        """电脑是否开机."""
        return self._on

    def reboot(self) -> str:
        """重启电脑 (关机后再开机). 所有实现通用."""
        off = self.power_off()
        on = self.power_on()
        return f"电脑[{self.role_id}] 已重启.\n- {off}\n- {on}"

    @property
    def workdir(self) -> str:
        """个人工作目录 (电脑上的路径). 子类可覆盖."""
        return "/home/agent"

    @property
    def drive_root(self) -> str:
        """企业云盘挂载根路径 (容器内 /mnt/drive; Local 降级为本地 data/drive)."""
        return "/mnt/drive"

    def describe(self) -> str:
        """电脑状态描述 (供 LLM 查看)."""
        return (f"电脑[{self.role_id}] ({self.__class__.__name__}): "
                f"状态={'开机' if self._on else '关机'}, 工作目录={self.workdir}")


# ── LocalComputer (本地目录模拟) ──────────────────────────

class LocalComputer(Computer):
    """本地目录模拟电脑 (开发/降级用).

    工作目录: data/computers/<role_id>/, 命令用 subprocess 在本地执行.
    """

    def __init__(self, role_id: str, base_dir: str = "./data/computers",
                 auto_mcp: bool = False):
        super().__init__(role_id, auto_mcp=auto_mcp)
        self._dir = Path(base_dir).resolve() / (role_id or "shared")
        self._dir.mkdir(parents=True, exist_ok=True)
        self._on = True  # 本地模拟默认开机

    @property
    def host_dir(self) -> str:
        # 本地电脑: 工作目录即宿主机目录 (MCP 服务器直接授权它)
        return str(self._dir)

    @property
    def workdir(self) -> str:
        return str(self._dir)

    @property
    def drive_root(self) -> str:
        # Local 降级: 云盘即本地共享目录 (权限语义退化为宿主机单用户)
        return str((Path("./data/drive")).resolve())

    def power_on(self) -> str:
        self._on = True
        self._dir.mkdir(parents=True, exist_ok=True)
        return f"电脑[{self.role_id}] (本地模拟) 已开机. 工作目录: {self._dir}"

    def power_off(self) -> str:
        self._on = False
        return f"电脑[{self.role_id}] (本地模拟) 已关机."

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self._dir / p
        return p

    def run_command(self, command: str) -> str:
        if not self._on:
            return "错误: 电脑未开机."
        try:
            result = subprocess.run(
                command, shell=True, cwd=self._dir, capture_output=True,
                text=True, timeout=30,
            )
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                return f"[exit {result.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except subprocess.TimeoutExpired:
            return "错误: 命令超时 (30s)."
        except Exception as exc:
            return f"错误: {exc}"

    def read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p.exists():
            return f"文件不存在: {p}"
        return p.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return str(p)

    def list_dir(self, path: str = "") -> str:
        p = self._resolve(path)
        if not p.exists() or not p.is_dir():
            return f"目录不存在: {p}"
        entries = sorted(x.name for x in p.iterdir())
        return "\n".join(entries) if entries else "(空目录)"

    def delete_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p.exists():
            return f"文件不存在: {p}"
        p.unlink()
        return f"已删除: {p}"


# ── PodmanComputer (podman 容器虚拟电脑, 默认) ────────────

class PodmanComputer(Computer):
    """Podman 容器虚拟电脑.

    每个角色一个容器 (名 maf-<role_id>), 命令经 podman exec 执行.
    需要本机安装 podman; 未安装时构造直接抛 RuntimeError
    (如需本地模拟请显式用 create_computer(kind='local')).

    参数:
        role_id: 角色标识.
        image:   容器镜像 (默认 alpine:latest).
    """

    def __init__(self, role_id: str, image: str = DEFAULT_IMAGE,
                 auto_mcp: bool = False, username: str = "agent",
                 uid: int = 1100, name: str = ""):
        super().__init__(role_id, auto_mcp=auto_mcp)
        self.image = image
        self.username = username or "agent"      # 容器内用户名 = 员工名字的汉语拼音
        self.uid = int(uid) or 1100              # 容器内 uid (文件所有权区分)
        self._is_ceo = (role_id or "").upper() == "CEO"  # Public 目录属主 (CEO 的用户)
        self.name = name                         # 角色中文名 (云盘个人目录名)
        self.container_name = f"maf-{role_id or 'shared'}"
        self._mcp_pkg_installed = False  # 容器内是否已预装 MCP 包 (真实属性)
        if shutil.which("podman") is None:
            raise RuntimeError(
                f"Podman 未安装, 无法创建角色 {role_id} 的电脑容器 "
                f"(PodmanComputer 需要 podman; 如需本地模拟请显式使用 "
                f"create_computer(kind='local'))."
            )

    @property
    def _drive_dir_name(self) -> str:
        """云盘个人目录名: 员工名字 (根目录文件夹 = 各角色名字)."""
        return self.name or self.username

    @property
    def host_dir(self) -> str:
        # 容器挂载的宿主机目录: data/computers/<role> ↔ 容器内 /home/<username>
        return str((Path("./data/computers").resolve() / (self.role_id or "shared")))

    @property
    def workdir(self) -> str:
        # 工作目录 = 员工家目录 (容器内用户名 = 拼音)
        return f"/home/{self.username}"

    def get_lan_ip(self) -> str:
        """获取本电脑在自定义桥接网络 (maf-net) 中的 IP 地址.

        返回:
            IP 字符串; 查不到返回空串.
        """
        try:
            # 网络名含连字符, Go template 必须用 index 取 (直接 .maf-net 会被当减号)
            fmt = '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % DEFAULT_NETWORK_NAME
            r = self._pod("inspect", self.container_name, "-f", fmt)
            ip = (r.stdout or "").strip()
            return ip or ""
        except Exception:
            logger.warning("电脑[%s] 获取内网 IP 失败", self.role_id, exc_info=True)
            return ""

    def install_mcp_server(self) -> list[str]:
        """在本电脑 (容器) 内安装独立的 MCP 服务器.

        C 方案: MCP 服务器跑在容器内 (podman exec -i 保持 stdio 管道),
        授权目录 = 容器内 workdir (/home/agent) — 与 LLM 看到的路径字面一致,
        不再有宿主机/容器路径空间不一致的问题.
        """
        if self._mcp_server is not None:
            return self.list_installed_mcp_tools()
        if not self._auto_mcp:
            logger.info("电脑[%s] 非自动创建, 不自动安装 MCP 服务器", self.role_id)
            return []

        try:
            from src.python_tools.mcp_toolkit import MCPServer
            # 容器内启动 filesystem 服务器: podman exec -i <容器> <node 直启> /home/agent
            # -i 保持 stdin/stdout 管道, MCP stdio 协议走容器内进程
            self._ensure_container()  # 确保容器运行 + 包已预装
            # 容器内直接 node 启动服务器: 绕过 npx -y 的 npm registry 检查
            # (npx 每次启动都查 registry, 单次 ~10s, 40 角色并发会严重拖慢加载;
            # 包已由 _ensure_container 预装到 /usr/local/bin, node 直启秒起)
            self._mcp_server = MCPServer(
                package=MCP_FILESYSTEM_PACKAGE,
                args=[self.workdir],  # 授权容器内工作目录
                command="podman",
                # 以员工用户运行: 文件写入按该用户 uid 判定 (云盘权限一致)
                command_args=["exec", "-i", "--user", self.username,
                              self.container_name, "node",
                              "/usr/local/bin/mcp-server-filesystem", self.workdir],
            )
            self._mcp_server.connect()
            tools = self._mcp_server.list_tools()
            from src.core.tools import ToolDef
            for tool in tools:
                tname = getattr(tool, "name", "")
                if not tname:
                    continue
                server = self._mcp_server

                def _make_handler(srv=server, tn=tname):
                    def handler(args: dict[str, Any]) -> str:
                        return srv.call_tool(tn, args)
                    return handler

                self._mcp_tools[tname] = ToolDef(
                    name=tname,
                    description=getattr(tool, "description", "") or "",
                    input_schema=getattr(tool, "input_schema", {}) or {},
                    handler=_make_handler(),
                    source=f"mcp:{MCP_FILESYSTEM_PACKAGE} (容器内 {self.container_name})",
                )
            logger.info("电脑[%s] 容器内 MCP 服务器已安装, %d 个工具: %s",
                        self.role_id, len(self._mcp_tools),
                        self.list_installed_mcp_tools())
        except Exception as exc:
            self._connect_error = str(exc)  # 记录失败原因 (诊断用, 下次可重试)
            logger.exception("电脑[%s] 容器内 MCP 服务器安装失败", self.role_id)
            return []
        return self.list_installed_mcp_tools()

    def _pod(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """执行 podman 命令."""
        return subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=timeout,
        )

    def _ensure_container(self) -> None:
        """确保容器存在并运行 (不存在则创建), 并创建工作目录/用户/云盘目录.

        每个 podman 调用都检查 returncode — 失败立即抛错, 不再静默
        吞掉 (否则系统照常运行, 笔记/总结实际没落盘却谎报已保存).

        容器内用户名 = 员工名字的汉语拼音 (如 guoxiaodong), 每员工一个
        固定 uid (1100+注册序): 所有 exec 命令以该用户身份执行, 企业云盘
        (挂载 /mnt/drive) 的文件所有权按 uid 区分 — 权限由 Linux 文件系统管理.
        """
        # 容器挂载宿主机目录: data/computers/<role> ↔ 容器内 /home/<username>
        host_dir = self.host_dir
        Path(host_dir).mkdir(parents=True, exist_ok=True)
        # 企业云盘共享目录 (所有容器挂载同一份): data/drive ↔ /mnt/drive
        drive_host = _DRIVE_HOST
        Path(drive_host).mkdir(parents=True, exist_ok=True)
        # 共享 npm 全局缓存: 挂载到容器 /root/.npm, 新容器预装 MCP 包时
        # 命中缓存秒装 (首个容器下载一次, 其余容器从缓存解压 — 40 角色并行
        # 加载时避免 40 次 npm 网络下载)
        Path(_NPM_CACHE_HOST).mkdir(parents=True, exist_ok=True)
        r = self._pod("ps", "-a", "--format", "{{.Names}}")
        # 精确名字检查: 不能依赖 --filter name=X (子串/正则匹配,
        # maf-tester_1 会误匹配 maf-tester_12 → 误判存在跳过创建)
        names = set((r.stdout or "").splitlines())
        if self.container_name not in names:
            # 加入自定义桥接网络 (电脑间互通), 网络不存在则先创建
            network = _COMPUTER_MANAGER.ensure_network()
            # rootless podman 并行创建容器时 storage 层偶发竞态: 名字检查
            # 通过后 run 仍可能报 "name already in use by external entity"
            # (半成品 storage 记录/瞬时锁). 清理残留后重试, 最多 3 次.
            for attempt in range(1, 4):
                r = self._pod("run", "-d", "--name", self.container_name,
                              "--network", network,
                              "-v", f"{host_dir}:{self.workdir}",
                              "-v", f"{drive_host}:/mnt/drive",
                              "-v", f"{_NPM_CACHE_HOST}:/root/.npm",
                              self.image, "sleep", "infinity")
                if r.returncode == 0:
                    break
                logger.warning(
                    "电脑[%s] podman run 第 %d 次失败 (%s), 清理残留后重试",
                    self.role_id, attempt,
                    (r.stderr or r.stdout or "").strip()[:200])
                # 清理该名字的半成品容器/残留记录 (不存在时 rm 报错忽略)
                self._pod("rm", "-f", self.container_name)
                time.sleep(1.0)
            if r.returncode != 0:
                raise RuntimeError(
                    f"podman run 创建容器失败 ({r.returncode}): "
                    f"{(r.stderr or r.stdout or '').strip()[:300]}")
        r = self._pod("ps", "--filter", f"name={self.container_name}", "--format", "{{.Names}}")
        if self.container_name not in (r.stdout or ""):
            r = self._pod("start", self.container_name)
            if r.returncode != 0:
                raise RuntimeError(
                    f"podman start 启动容器失败 ({r.returncode}): "
                    f"{(r.stderr or r.stdout or '').strip()[:300]}")
        # 容器内员工用户: 名字的汉语拼音 + 固定 uid; 家目录属主 = 该用户
        # (挂载的宿主目录默认 root 所有, 不 chown 员工写不进去)
        setup = (
            f"grep -q '^{shlex.quote(self.username)}:' /etc/passwd || "
            f"adduser -D -u {self.uid} -s /bin/sh {shlex.quote(self.username)} 2>/dev/null; "
            f"mkdir -p {shlex.quote(self.workdir)}; "
            f"chown -R {self.uid}:{self.uid} {shlex.quote(self.workdir)}"
        )
        r = self._pod("exec", self.container_name, "sh", "-c", setup)
        if r.returncode != 0:
            raise RuntimeError(
                f"podman exec 创建用户失败 ({r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()[:300]}")
        # 企业云盘初始化 (容器内 root 建目录 + 权限/属主):
        #   - Public: 777 (公用共享), 属主 = CEO 的用户
        #   - <本人名字>/: 755, 属主 = 本人 uid (其他员工只读, 本人可写)
        drive_init = (
            f"mkdir -p /mnt/drive/Public {shlex.quote('/mnt/drive/' + self._drive_dir_name)}; "
            f"chmod 777 /mnt/drive/Public; "
            f"chmod 755 {shlex.quote('/mnt/drive/' + self._drive_dir_name)}; "
            f"chown {self.uid}:{self.uid} {shlex.quote('/mnt/drive/' + self._drive_dir_name)}"
        )
        if self._is_ceo:
            drive_init += f"; chown {self.uid}:{self.uid} /mnt/drive/Public"
        r = self._pod("exec", self.container_name, "sh", "-c", drive_init)
        if r.returncode != 0:
            raise RuntimeError(
                f"podman exec 初始化云盘失败 ({r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()[:300]}")
        # 预装 MCP filesystem 服务器包 (容器内全局安装, 之后启动即用, 免 npx 拉包)
        if not self._mcp_pkg_installed:
            r = self._pod("exec", self.container_name, "sh", "-c",
                          "npm ls -g --depth=0 2>/dev/null | grep -q 'server-filesystem' "
                          "|| npm install -g --no-fund --no-audit "
                          f"{shlex.quote(MCP_FILESYSTEM_PACKAGE)}", timeout=300)
            if r.returncode != 0:
                raise RuntimeError(
                    f"容器内预装 MCP filesystem 包失败 ({r.returncode}): "
                    f"{(r.stderr or r.stdout or '').strip()[:300]}")
            self._mcp_pkg_installed = True
            logger.info("电脑[%s] 容器内已预装 MCP filesystem 服务器 (npm -g)",
                        self.role_id)

    def power_on(self) -> str:
        try:
            self._ensure_container()
            self._on = True
            # 跨天重连: 容器 stop 会杀死 MCP stdio 管道 (podman exec -i),
            # 开机后检测会话存活, 死了就重建, 否则第 2 天起 MCP 文件工具全坏
            self._reconnect_mcp_server()
            return (f"电脑[{self.role_id}] (podman 容器 {self.container_name}) 已开机. "
                    f"工作目录: {self.workdir}")
        except Exception as exc:
            return f"错误: 开机失败 - {exc}"

    def power_off(self) -> str:
        try:
            self._pod("stop", self.container_name)
            self._on = False
            return f"电脑[{self.role_id}] (podman) 已关机."
        except Exception as exc:
            return f"错误: 关机失败 - {exc}"

    def run_command(self, command: str) -> str:
        if not self._on:
            return "错误: 电脑未开机."
        try:
            # 以员工用户执行 (容器内用户名 = 拼音): 云盘/家目录权限按该用户判定
            r = self._pod("exec", "--user", self.username,
                          self.container_name, "sh", "-c", command)
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return f"[exit {r.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except Exception as exc:
            return f"错误: 命令执行失败 - {exc}"

    def read_file(self, path: str) -> str:
        # 路径经 argv 传入 (sh -c 的 $1), 不经 shell 解析 — 任何字符都按字面,
        # 无注入面 (High-3). shlex.quote 方案在 busybox ash 对
        # 单引号+空格+$()+反引号组合的路径会解析出错, 这里彻底绕开.
        return self._exec_argv("cat -- \"$1\"", path)

    def write_file(self, path: str, content: str) -> str:
        """写入容器内文件: 内容经 stdin (exec -i), 路径经 argv ($1/$2).

        - 内容不进 shell 命令 (无 heredoc/无转义) — 二进制安全
        - 父目录与路径都作为独立 argv 传给 sh -c, 不经 shell 引号解析,
          含单引号/反引号/$()/空格的字面路径也不会执行或被截断
        """
        if not self._on:
            return "错误: 电脑未开机."
        parent = str(Path(path).parent) if "/" in path else "."
        try:
            r = subprocess.run(
                ["podman", "exec", "-i", "--user", self.username,
                 self.container_name, "sh", "-c",
                 'mkdir -p -- "$2" && cat > "$1"', "sh", path, parent],
                capture_output=True, text=True, timeout=60, input=content,
            )
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return f"[exit {r.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except Exception as exc:
            return f"错误: 命令执行失败 - {exc}"

    def _exec_argv(self, script: str, *args: str, timeout: int = 60) -> str:
        """以 argv 方式执行容器内命令 (脚本 + 参数分离, 路径不经 shell 解析).

        参数:
            script: sh -c 的脚本 (用 $1/$2... 引用参数).
            *args:  传给脚本的位置参数 (字面透传, 不经过 shell 引号).
        """
        if not self._on:
            return "错误: 电脑未开机."
        try:
            r = subprocess.run(
                ["podman", "exec", "--user", self.username,
                 self.container_name, "sh", "-c", script,
                 "sh", *args],
                capture_output=True, text=True, timeout=timeout,
            )
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return f"[exit {r.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except Exception as exc:
            return f"错误: 命令执行失败 - {exc}"

    def list_dir(self, path: str = "") -> str:
        target = path or self.workdir
        return self._exec_argv("ls -la -- \"$1\"", target)

    def delete_file(self, path: str) -> str:
        return self._exec_argv("rm -f -- \"$1\"", path)

    def describe(self) -> str:
        return (f"电脑[{self.role_id}] (podman 容器 {self.container_name}): "
                f"状态={'开机' if self._on else '关机'}, 工作目录={self.workdir}")


# ── SSHComputer (远程主机) ────────────────────────────────

class SSHComputer(Computer):
    """SSH 远程电脑.

    通过 ssh 在远程主机上执行命令. 需要 host/user 配置.
    工作目录: ~/maf-<role_id>/ (自动创建).

    参数:
        role_id: 角色标识.
        host:    远程主机 (必填).
        user:    登录用户 (默认当前用户).
        key_path: 私钥路径 (可选, 默认用 ssh-agent/默认密钥).
        port:    ssh 端口 (默认 22).
    """

    def __init__(
        self,
        role_id: str,
        host: str,
        user: Optional[str] = None,
        key_path: Optional[str] = None,
        port: int = 22,
        auto_mcp: bool = False,
    ):
        super().__init__(role_id, auto_mcp=auto_mcp)
        if not host:
            raise ValueError("SSHComputer 需要 host 参数 (远程主机地址)")
        self.host = host
        self.user = user
        self.key_path = key_path
        self.port = port

    @property
    def workdir(self) -> str:
        return f"~/maf-{self.role_id or 'shared'}"

    def _ssh(self, remote_cmd: str, timeout: int = 60) -> str:
        """执行远程命令, 返回输出文本."""
        target = self.host
        if self.user:
            target = f"{self.user}@{target}"
        cmd = ["ssh", "-p", str(self.port), "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10"]
        if self.key_path:
            cmd += ["-i", self.key_path]
        cmd += [target, f"mkdir -p {self.workdir} && cd {self.workdir} && {remote_cmd}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return f"[exit {r.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except subprocess.TimeoutExpired:
            return "错误: ssh 命令超时 (60s)."
        except Exception as exc:
            return f"错误: ssh 执行失败 - {exc}"

    def power_on(self) -> str:
        # ssh 无"开机"概念, 建立会话即视为开机
        r = self._ssh("echo ok")
        if "ok" in r:
            self._on = True
            return f"电脑[{self.role_id}] (ssh {self.host}) 已连接. 工作目录: {self.workdir}"
        return f"错误: 无法连接 {self.host}: {r}"

    def power_off(self) -> str:
        self._on = False
        return f"电脑[{self.role_id}] (ssh) 已断开."

    def run_command(self, command: str) -> str:
        if not self._on:
            return "错误: 电脑未开机."
        return self._ssh(command)

    def read_file(self, path: str) -> str:
        # shlex.quote: 防路径中的单引号/反引号/$() 闭合注入 (与 Podman 版一致)
        return self._ssh(f"cat {shlex.quote(path)}")

    def write_file(self, path: str, content: str) -> str:
        """写入远程文件: 内容经 stdin 传入 ssh, 路径 shlex.quote (同 Podman 版)."""
        if not self._on:
            return "错误: 电脑未开机."
        q = shlex.quote
        parent = str(Path(path).parent) if "/" in path else "."
        target = self.host
        if self.user:
            target = f"{self.user}@{target}"
        cmd = ["ssh", "-p", str(self.port), "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10"]
        if self.key_path:
            cmd += ["-i", self.key_path]
        cmd += [target, f"mkdir -p {self.workdir} && cd {self.workdir} && "
                        f"mkdir -p {q(parent)} && cat > {q(path)}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                               input=content)
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return f"[exit {r.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except subprocess.TimeoutExpired:
            return "错误: ssh 命令超时 (60s)."
        except Exception as exc:
            return f"错误: ssh 执行失败 - {exc}"

    def list_dir(self, path: str = "") -> str:
        target = path or self.workdir
        return self._ssh(f"ls -la {shlex.quote(target)}")

    def delete_file(self, path: str) -> str:
        return self._ssh(f"rm -f {shlex.quote(path)}")


# ── 工厂 ──────────────────────────────────────────────────

def create_computer(kind: str = "podman", role_id: str = "", *,
                    auto_mcp: bool = False, **kwargs: Any) -> Computer:
    """按类型创建电脑实例.

    参数:
        kind:     "podman" (默认) | "ssh" | "local".
        role_id:  角色标识.
        auto_mcp: 是否自动创建的电脑实例. True = 创建实例时自动安装独立的
                  MCP 服务器 (AgentRole.computer 自动创建时传 True);
                  False = 不自动安装 (手动 create_computer 调用).
        kwargs:   透传给具体实现 (ssh 需 host/user 等).

    返回:
        Computer 实例.
    """
    kind = (kind or "podman").lower()
    if kind == "local":
        return LocalComputer(role_id=role_id, auto_mcp=auto_mcp)
    if kind == "ssh":
        if not kwargs.get("host"):
            raise ValueError("SSHComputer 需要 host 参数 (远程主机地址)")
        return SSHComputer(role_id=role_id, auto_mcp=auto_mcp, **kwargs)
    return PodmanComputer(role_id=role_id, auto_mcp=auto_mcp, **kwargs)


# ── ComputerManager (电脑管理类) ──────────────────────────

DEFAULT_NETWORK_NAME = "maf-net"  # podman 自定义桥接网络 (电脑间互通)

# 企业云盘共享目录 (所有角色容器挂载同一份到 /mnt/drive):
#   Public/   = 公用共享 (777, 属主 CEO 的用户)
#   <名字>/   = 各员工个人目录 (755, 属主 = 对应员工)
_DRIVE_HOST = str((Path("./data/drive")).resolve())

# 共享 npm 全局缓存目录 (挂载进每个角色容器): 容器预装 MCP 包时命中缓存,
# 避免 40 个新容器各自 npm 网络下载 (data/ 整体 gitignored, 不入库)
_NPM_CACHE_HOST = str((Path("./data/computers") / ".npm-cache").resolve())


class ComputerManager:
    """电脑管理类: 分配 / 注册 / 查询 / 销毁 各角色电脑.

    职责:
      - 维护角色 → 电脑的注册表 (含人名, 供内网设备列表展示)
      - 确保 podman 自定义桥接网络存在 (电脑间可互相通信)
      - 统一销毁入口 (关机 + 删除容器 + 注销)
      - 查询内网电脑设备 (人名 / 电脑名 / IP)

    全局单例 _COMPUTER_MANAGER (computer.py 末尾), AgentRole.computer
    自动创建的电脑都会注册进来; 手动 create_computer() 创建的不会.
    """

    def __init__(self, network_name: str = DEFAULT_NETWORK_NAME):
        self.network_name = network_name
        self._computers: dict[str, Any] = {}   # role_id → Computer
        self._names: dict[str, str] = {}       # role_id → 人名
        # podman 网络检查/创建加锁: 多角色并行装配电脑时, 防止 40 个线程
        # 同时探测到网络不存在 → 并发重复 create 的竞态
        self._network_lock = threading.Lock()

    # ── 网络 ──────────────────────────────────────────────

    def ensure_network(self) -> str:
        """确保 podman 自定义桥接网络存在 (幂等). 返回网络名.

        网络用于让各角色电脑 (容器) 之间可以互相通信.
        本机无 podman 时直接返回网络名 (不实际创建, 降级环境无网络).
        加锁: 多角色并行装配时只有一个线程真正执行创建.
        """
        if shutil.which("podman") is None:
            return self.network_name
        with self._network_lock:
            r = subprocess.run(["podman", "network", "exists", self.network_name],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                subprocess.run(["podman", "network", "create", self.network_name],
                               capture_output=True, text=True, timeout=60)
                logger.info("podman 自定义桥接网络已创建: %s", self.network_name)
        return self.network_name

    # ── 分配 / 注册 ───────────────────────────────────────

    def create(self, kind: str = "podman", role_id: str = "", name: str = "",
               auto_mcp: bool = False, **kwargs: Any) -> Any:
        """创建并注册一台角色电脑 (分配).

        参数:
            kind:     电脑类型 ("podman" 默认 | "ssh" | "local").
            role_id:  角色 ID (注册表键).
            name:     人名 (供内网设备列表展示, 可空).
            auto_mcp: 是否自动安装独立 MCP 服务器.
            kwargs:   透传给 create_computer.

        返回:
            Computer 实例.
        """
        self.ensure_network()
        comp = create_computer(kind=kind, role_id=role_id,
                               auto_mcp=auto_mcp, name=name, **kwargs)
        self.register(comp, name=name)
        return comp

    def register(self, computer: Any, name: str = "") -> None:
        """注册一台已创建的电脑到管理器."""
        self._computers[computer.role_id] = computer
        if name:
            self._names[computer.role_id] = name

    # ── 查询 ──────────────────────────────────────────────

    def get(self, role_id: str) -> Any:
        """按角色 ID 获取电脑 (不存在抛 KeyError)."""
        return self._computers[role_id]

    def list_all(self) -> list[Any]:
        """返回全部已注册电脑列表 (按注册顺序)."""
        return list(self._computers.values())

    # ── 销毁 ──────────────────────────────────────────────

    def destroy(self, role_id: str) -> bool:
        """销毁角色电脑: 关机 + 删除容器 + 注销. 返回是否销毁成功.

        参数:
            role_id: 角色 ID.
        """
        comp = self._computers.pop(role_id, None)
        self._names.pop(role_id, None)
        if comp is None:
            return False
        # 关机
        try:
            if comp.is_on:
                comp.power_off()
        except Exception:
            logger.warning("电脑[%s] 关机失败 (销毁继续)", role_id, exc_info=True)
        # 删除 podman 容器 (仅真实容器; LocalComputer 无容器)
        if isinstance(comp, PodmanComputer):
            try:
                comp._pod("rm", "-f", comp.container_name)
                logger.info("电脑[%s] 容器已删除: %s", role_id, comp.container_name)
            except Exception:
                logger.warning("电脑[%s] 容器删除失败", role_id, exc_info=True)
        logger.info("电脑[%s] 已销毁 (注销)", role_id)
        return True

    # ── 内网设备 ──────────────────────────────────────────

    def list_lan_devices(self) -> list[dict[str, str]]:
        """列出内网电脑设备: 人名 / 电脑名 / IP.

        返回:
            [{"person", "role_id", "computer", "ip"}, ...] 按角色排序.
        """
        devices = []
        for role_id, comp in sorted(self._computers.items()):
            ip = ""
            if hasattr(comp, "container_name"):
                # podman 容器: 查网络内 IP
                ip = comp.get_lan_ip() if hasattr(comp, "get_lan_ip") else ""
            elif hasattr(comp, "host"):
                ip = comp.host  # ssh 电脑: 远程主机地址
            devices.append({
                "person": self._names.get(role_id, role_id),
                "role_id": role_id,
                "computer": getattr(comp, "container_name",
                                    f"local-{role_id.lower()}"),
                "ip": ip or "(无内网IP)",
            })
        return devices


# 全局单例: 角色自动创建的电脑统一注册到这里
_COMPUTER_MANAGER = ComputerManager()
