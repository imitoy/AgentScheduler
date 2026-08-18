"""通信工具类 (Communication ToolKit).

包含:
  - talk:       角色之间的消息传递与任务委托
  - list_roles: 获取当前团队角色列表 (与 talk 花名册格式一致)

团队花名册格式固定, 由 build_team_roster() 统一生成.

用法:
    from src.python_tools.talk_toolkit import create_talk_toolkit
    tk = create_talk_toolkit(pool)       # pool = RolePool 实例
    role.add_toolkit(tk)                  # 角色导入整个工具类
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit
from src.core.types import AgentState

logger = logging.getLogger(__name__)


def build_team_roster(pool: Any) -> str:
    """构建团队花名册 (固定格式, 供 talk 描述与 list_roles 工具复用).

    ⚠️ 只暴露人名: 花名册是给 LLM 看的, 内部 role_id (索引) 一律不出现.

    格式:
        - **姓名** -- 职责  Skills: 技能列表

    参数:
        pool: RolePool 实例.

    返回:
        花名册字符串 (每行一个成员).
    """
    roster_lines: list[str] = []
    for _rid, r in pool._roles.items():
        resp = r.responsibilities or r.title
        roster_lines.append(
            f"  - **{r.name}** -- {resp}  "
            f"Skills: {', '.join(r.skills[:4])}"
        )
    return "\n".join(roster_lines)


def _would_deadlock(pool: Any, start_role: Any, sender_id: str) -> bool:
    """沿 WAIT 等待链查环: start_role 的等待链上若绕回 sender_id 则成环 (互等死锁).

    例: A wait B, B wait C, C wait A → 任一角色再发起 wait 都会被本检测拒绝.
    双向互等 (A wait B 且 B wait A) 已被"回复投递"规则优先拆解, 这里兜底
    处理 3 个及以上角色的环形等待.

    参数:
        pool: RolePool (查等待链上的角色).
        start_role: 目标角色 (发送者想 wait 的对象).
        sender_id: 发送者 role_id.

    返回:
        True = 构成等待环, 应拒绝 wait.
    """
    seen = set()
    cur = start_role
    while cur is not None and cur.state == AgentState.WAIT and cur._waiting_reply_from:
        if cur.role_id in seen:
            return True  # 等待链自身成环 (理论不可达, 防御)
        seen.add(cur.role_id)
        nxt_id = cur._waiting_reply_from
        if nxt_id == sender_id:
            return True  # 等待链绕回发送者 → 互等死锁
        cur = pool._roles.get(nxt_id)
    return False


def create_talk_toolkit(pool: Any) -> ToolKit:
    """创建通信工具类 (talk + list_roles).

    参数:
        pool: RolePool 实例, 用于查找目标角色并投递任务.

    返回:
        包含 talk / list_roles 工具的 ToolKit 实例.
    """
    from src.core.roles import Task, Urgency

    tk = ToolKit(name="communication", description="角色间通信工具类")

    def _talk_handler(args: dict[str, Any]) -> str:
        """talk 工具处理函数: 发送消息/委托任务, 可选 wait=true 同步等待回复.

        参数:
            args: {"target": 目标role_id, "message": 消息内容,
                   "urgency": 紧急度, "wait": 是否等待回复 (默认 false)}

        wait=true 流程:
            1. 发送方角色状态 → WAIT (记录原状态)
            2. 消息入目标角色队列 (消息附带"提问者正在等待"提示, 目标知情)
            3. 发送方 worker 阻塞等待, 目标角色处理消息后调 talk 回复
               (回复投递: 目标 WAIT 且等待对象是发送者 → 直接进回复信箱唤醒)
            4. 收到回复 → 恢复原状态, 返回回复内容给 LLM
            等待无时间限制 (LLM 输出时间不可预测, 发送方会一直等到回复).

        死锁防护 (多个角色同时 wait=true):
            - 回复投递优先: 目标在等自己 → 一律视为回复, 不进入等待
            - 环形等待检测: 发起 wait 前沿等待链查环, 成环则拒绝
            - 等待提示: 消息明确告知对方自己在等待, 促其尽快回复

        返回:
            发送结果字符串 (含对方队列深度或对方回复内容).
        """
        target = args.get("target", "")
        message = args.get("message", "")
        urgency_str = args.get("urgency", "NORMAL")
        # attachment: 企业云盘路径 (指向共享文件夹 /mnt/drive 下内容, 发送时校验可读)
        attachment = (args.get("attachment") or "").strip() or None
        # wait 参数: 模型可能传布尔或字符串 ("true"/"false"), 统一解析
        wait_val = args.get("wait", False)
        if isinstance(wait_val, str):
            wait = wait_val.lower() in ("1", "true", "yes", "on")
        else:
            wait = bool(wait_val)

        if not target or not message:
            return "错误: 'target' 和 'message' 为必填参数."

        # target 是人名 (LLM 视角, 见 list_roles 花名册); 内部按人名→role_id
        # 映射 (get_role_by_name 兼容 role_id 回退, 供编程直调)
        target_role = pool.get_role_by_name(target)
        if target_role is None:
            return (f"错误: 团队中找不到 '{target}'。"
                    f"请先调用 list_roles 查看当前成员姓名, 再用人名发送。")
        urgency = getattr(Urgency, urgency_str.upper(), Urgency.NORMAL)
        sender = getattr(tk, "_role_holder", None)
        sender = sender.get("role") if sender is not None else None
        sender_id = sender.role_id if sender is not None else None

        # ── 附件校验: 云盘路径必须存在且当前角色可读 (容器内以本人身份判定) ──
        if attachment is not None:
            if sender is None:
                return "错误: 当前角色未绑定, 无法发送附件 (attachment)."
            if (".." in attachment.split("/") or attachment.startswith("/")
                    or attachment.endswith("/")):
                return f"错误: 附件路径非法: '{attachment}' (须为云盘相对路径)"
            try:
                comp = sender.computer  # 惰性开机 (容器内以员工身份读取)
                content = comp.read_file(f"{comp.drive_root}/{attachment}")
            except Exception as exc:
                return f"错误: 附件不可读: {exc}"
            if content.startswith(("[exit", "错误", "文件不存在")):
                return f"错误: 附件无效: 云盘文件不存在或不可读 '{attachment}'"

        # ── 1) 回复投递: 目标正处于 WAIT 且在等我回复 → 直接投递唤醒 ──
        #    等待者 worker 阻塞中, 回复绝不能入队 (否则永远收不到);
        #    wait 参数在此被忽略 — 回复本身不进入等待, 从根上拆解双向互等.
        if target_role.state == AgentState.WAIT \
                and target_role._waiting_reply_from == sender_id:
            target_role._deliver_reply(message)
            if sender is not None:
                sender.journal(
                    f"回复了等待中的 {target_role.name}: {message[:80]}")
            return f"已回复给正在等待的 {target_role.name}."

        # 构造任务: wait=true 时在消息中明确告知对方"提问者正在等待",
        # 让目标知道当前情况 (LLM 输出时间不可预测, 发送方会一直等到回复);
        # attachment 附件为云盘路径, 消息里提示对方可用 drive_read 读取
        waiting_hint = ""
        if wait and sender is not None:
            waiting_hint = (
                f"\n\n⚠️ {sender.name} 正在等待你的回复 (wait=true)。"
                f"请优先处理这条消息, 尽快用 talk 工具回复对方。"
            )
        attach_hint = ""
        if attachment:
            attach_hint = (
                f"\n[附件: {attachment}] (公司云盘文件, "
                f"在 /mnt/drive 下可直接读取)"
            )
        task = Task(
            urgency=urgency,
            description=f"[FROM talk] {message}{attach_hint}{waiting_hint}",
            source="talk",
            context={"message": message, "waiting": bool(wait),
                     "attachment": attachment},
        )

        # ── 2) wait=true: 无限等待对方回复 ──
        #    不做超时限制 (LLM 输出时间不可预测); 等待直到收到回复
        if wait:
            if sender is None:
                return "错误: 当前角色未绑定, 无法使用 wait=true。"
            # 死锁防护: 目标正处于 WAIT, 沿其等待链查环 (A等B→B等C→C等A)
            if target_role.state == AgentState.WAIT and _would_deadlock(
                    pool, target_role, sender.role_id):
                return ("错误: 检测到互相等待死锁 (对方的等待链成环回到你)。"
                        "请勿使用 wait=true, 改为普通消息或稍后再询问。")
            # 先进入 WAIT 再发消息: 防止对方秒回时回复投递条件不成立 (竞态)
            # 等待链统一用 role_id 判断 (LLM 参数用人名, 程序内部用 role_id)
            sender._begin_wait(target_role.role_id)
            try:
                target_role.add_task(task)
                sender.journal(
                    f"发消息给 {target_role.name} ({urgency.name}, 等待回复): "
                    f"{message[:80]}")
                reply = sender._wait_for_reply()  # 无限等待回复
            finally:
                sender._end_wait()
            return f"已收到 {target_role.name} 的回复: {reply}"

        # ── 3) 普通消息: 入目标队列, 立即返回 ──
        target_role.add_task(task)
        if sender is not None:
            sender.journal(
                f"发消息给 {target_role.name} ({urgency.name}): {message[:80]}")
        return (
            f"消息已发送给 {target_role.name}, 紧急度={urgency.name}, "
            f"对方队列现有 {target_role.queue_depth} 个任务."
        )

    def _list_roles_handler(args: dict[str, Any]) -> str:
        """list_roles 工具处理函数: 实时获取当前团队角色列表.

        参数:
            args: 无.

        返回:
            当前角色花名册 (姓名/role_id/职责/技能).
        """
        roster = build_team_roster(pool)  # 动态构建, 包含新入职角色
        if not roster:
            return "(当前无团队成员)"
        return f"当前团队成员:\n{roster}"

    tk.add_python_tool(
        name="talk",
        description=(
            "给团队成员发送消息或委托任务. "
            "团队当前有哪些成员请先调用 list_roles 获取 (名单是动态的, 可能有新入职). "
            "根据每个人的职责选择合适的人选后, 用 target 发送.\n"
            "target 参数使用成员姓名 (见 list_roles 花名册, 例如 '王建国').\n"
            "attachment 可选: 公司云盘文件路径 (如 'Public/方案.md' 或 '郭晓东/设计稿.md'), "
            "作为附件随消息发送 (文件在 /mnt/drive 下); 发送前系统会校验文件存在.\n"
            "wait=true 表示需要对方回复后才能继续 (同步等待): 你会进入 WAIT 状态, "
            "消息会附带'你正在等待回复'的提示, 对方收到后应尽快用 talk 回复你; "
            "收到回复后工具返回回复内容并恢复原状态. "
            "等待没有时间限制 (LLM 输出时间不可预测). "
            "仅在确实需要对方答案才能继续时才用 wait=true; "
            "普通通知/委托请用 wait=false (默认), 不要互相 wait 以免死锁."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "目标成员姓名 (团队名单请先通过 list_roles 获取, 花名册里的姓名)",
                },
                "message": {
                    "type": "string",
                    "description": "要发送的消息或委托的任务, 描述要具体.",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["LOW", "NORMAL", "HIGH", "CRITICAL"],
                    "description": "紧急程度, 生产事故用 CRITICAL.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "是否等待对方回复 (默认 false). true = 同步等待, 收到回复后继续.",
                },
                "attachment": {
                    "type": "string",
                    "description": "可选: 企业云盘文件路径 (如 'Public/方案.md'), 作为附件随消息发送",
                },
            },
            "required": ["target", "message"],
        },
        handler=_talk_handler,
    )

    tk.add_python_tool(
        name="list_roles",
        description=(
            "获取当前团队都有哪些成员 (姓名/职责/技能). "
            "在向同事发消息前, 或不确定该找谁处理某件事时, 先调用此工具查看团队成员, "
            "然后用 talk 给对应姓名发消息."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_list_roles_handler,
    )

    return tk
