"""角色中文名 → 汉语拼音 (系统用户名) 映射表.

Podman 容器用户名 = 员工名字的汉语拼音 (如 郭晓东 → guoxiaodong),
用于企业云盘 (共享文件夹挂载 /mnt/drive/) 的权限管理:
不同员工 = 不同容器内用户 = 不同 uid → 文件所有者可区分.

命名规则: 全拼小写无空格 (姓名连写), 与容器内 useradd 用户名一致.
"""

from __future__ import annotations

# 角色名 → 拼音 (48 个默认模板角色; 新角色未收录时回退用 role_id)
NAME_PINYIN: dict[str, str] = {
    "林总": "linzong",          # CEO
    "陈总": "chenzong",         # COO
    "王人事": "wangrenshi",     # HR
    "钱财": "qiancai",          # CFO
    "李明": "liming",           # fullstack_dev (模板)
    "王建国": "wangjianguo",    # architect
    "张伟": "zhangwei",         # reviewer
    "刘洋": "liuyang",          # qa_engineer
    "赵强": "zhaoqiang",        # ops_engineer
    "陈静": "chenjing",         # content_marketer
    "孙晓": "sunxiao",          # data_analyst
    "周梅": "zhoumei",          # support_agent
    "顾承宇": "guchengyu",      # frontend_dev_1
    "唐思远": "tangsiyuan",     # frontend_dev_2
    "罗子涵": "luozihan",       # frontend_dev_3
    "彭志强": "pengzhiqiang",   # backend_dev_1
    "萧文博": "xiaowenbo",      # backend_dev_2
    "邓立群": "dengliqun",      # backend_dev_3
    "曾子墨": "zengzimo",       # mobile_dev_1
    "卢俊豪": "lujunhao",       # mobile_dev_2
    "蔡文静": "caiwenjing",     # mobile_dev_3
    "谭志远": "tanzhiyuan",     # fullstack_dev_1
    "范晓峰": "fanxiaofeng",    # fullstack_dev_2
    "高梦洁": "gaomengjie",     # fullstack_dev_3
    "郭晓东": "guoxiaodong",    # tester_1
    "马春燕": "machunyan",      # tester_2
    "宋佳琪": "songjiaqi",      # tester_3
    "袁明轩": "yuanmingxuan",   # tester_4
    "胡婷婷": "hutingting",     # tester_5
    "石景山": "shijingshan",    # tester_6
    "程雪梅": "chengxuemei",    # tester_7
    "陆一帆": "luyifan",        # tester_8
    "孟浩然": "menghaoran",     # tester_9
    "沈佳宜": "shenjiayi",      # tester_10
    "田晓慧": "tianxiaohui",    # tester_11
    "魏莱": "weilai",           # tester_12
    "姜文博": "jiangwenbo",     # tester_13
    "谢婉婷": "xiewanting",     # tester_14
    "邹明": "zouming",          # tester_15
    "苏韵": "suyun",            # tester_16
    "潘志远": "panzhiyuan",     # tester_17
    "葛天宇": "getianyu",       # tester_18
    "薛静怡": "xuejingyi",      # tester_19
    "阮志明": "ruanzhiming",    # tester_20
    "白鹏": "baipeng",          # attacker_1
    "严冬": "yandong",          # attacker_2
    "纪安": "jian",             # attacker_3
    "方谨言": "fangjinyan",     # release_manager
    "高远": "gaoyuan",           # CTO
    "陈思远": "chensiyuan",      # frontend_lead
    "王宇轩": "wangyuxuan",      # backend_lead
    "李俊杰": "lijunjie",        # fullstack_lead
    "张雅婷": "zhangyating",     # mobile_lead
    "刘子涵": "liuzihan",        # test_lead
}
