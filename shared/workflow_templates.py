"""Owner-visible checklists, never executable scripts or model instructions."""
TEMPLATES = {
    "review_fix": {
        "name": "检查与修复",
        "steps": [
            {"title": "确认现状与问题", "acceptance": "记录实际读取的文件、现有修改和可复现的问题；不声称扫描未读文件。"},
            {"title": "实施修改", "acceptance": "保留用户已有修改，关联实际写入或执行记录，并说明影响范围。"},
            {"title": "回归验证", "acceptance": "关联本轮实际测试操作，检查退出码和输出；明确未验证的平台或场景。"},
        ],
    },
    "release": {
        "name": "验证与发布",
        "steps": [
            {"title": "检查发布范围", "acceptance": "确认工作区、目标分支、版本和用户授权的发布目标。"},
            {"title": "构建与测试", "acceptance": "关联实际构建与测试记录；失败先修复，不把提交成功当作测试通过。"},
            {"title": "发布与验收", "acceptance": "关联发布操作和目标环境检查，核实版本、运行状态与回滚方式。"},
        ],
    },
}
