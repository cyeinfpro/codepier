# CodePier MCP 上下文占用深度复查与优化建议（2026-11-10）

> 日期：2026-11-10（Asia/Shanghai）
>
> 范围：HEAD `46dd9224ac6b`（main，工作区另有 16 个与本文无关的未提交改动，未触碰）。经 Claude 连接实际调用统一目录 `query / act / get_profile`，并在项目 `.venv` 内对真实回包做字符测量。
>
> 与前序文档的关系：`MCP_PLUGIN_REVIEW_2026-11-09.md` 的 F01–F10 已实施，本文不重复，只写 **F01–F10 之后仍然存在的浪费**。
>
> 状态：仅建议，未改任何源码。文中带“已独立运行”标记的函数，已在本次测量脚本里对真实回包跑通；未接入主流程，未跑项目测试。

## 0. 结论

**问：工具现在是最优、极致精简了吗？**

- **工具目录层：接近最优。** 三个工具、按需 help、按权限裁剪动作，方向不需要再动。还能再挤掉约 27% 的 schema 字符，属于收尾。
- **回包层：没有。** 每次调用的固定信封、重复字段、未遵守 `.gitignore` 的目录、以及“正文双份计入预算”仍在平白消耗上下文。这一层是按调用次数重复付费的，比目录层重要得多。

按“每次任务实际多花多少上下文”排序，最值得做的五件事：

| 排名 | 问题 | 实测 | 建议后 |
| --- | --- | --- | --- |
| 1 | 读文件正文双份计入 49,152 字节预算，且超限后按“折半”截断 | 38.6KB 的文件一次只回 387/774 行，必须读两次 | 一次读完 |
| 2 | 根目录 tree 不遵守 `.gitignore`，首屏 300 行里 191 行是 `dist/` 发布包 | 首屏 11,764 字符，其中 8,740（74%）是噪声，且只覆盖根目录约 1/5 | 5,771 字符覆盖整个根目录 |
| 3 | 首次 `project open` 回包 | 5,530 字符 | 1,307（−76%） |
| 4 | 搜索回包逐条重复 `path` / `line_truncated`，外加每次一段约 230 字符的固定 `limits` | 40 条命中 6,992 字符，命中正文只有 2,753 | 3,494（−50%） |
| 5 | 读操作走“内联等待”路径时返回完整调度信封，且形态与直返不同 | 多 679 字符，模型要处理两种结构 | 与直返一致 |

以上五项的单次回包合计（open + 根 tree 首屏 + 40 条搜索 + 一次成功 exec）：24,690 → 10,725 字符（−56.6%）。这是几个实测样本的相加，不是完整任务的 token 节省比例。

项目地图（knowledge）自动维护的现状实现和补强方案单列在第 8 节。

## 1. 口径与边界

- **字符数**：`json.dumps(value, ensure_ascii=False, separators=(',', ':'))` 的长度，与前序文档一致。项目环境没有 tokenizer（`tiktoken` 未安装），本文**不给 token 数**。
- **实测**：直接取自本次调用回包（见第 7 节操作编号）。
- **重建**：项目列表按真实字段和行数重建（id 用等长占位）；根目录 tree 按工具实际列出的 12 个顶层目录在磁盘上重放。重建项在正文中标明。
- **目录层数字**来自 `tool_definitions(None, collaboration=True)`，包含本连接因权限未显示的 `devices`、`project_create`，比线上目录略大。
- **未验证**：各宿主是把 `content[0].text` 还是 `structuredContent` 还是两者都送进模型。本次 Claude 连接里模型看到的是 JSON 文本那一份。

## 2. 发现与建议

### C01 · 读文件：正文双份计入预算 + 折半截断 · P0

**证据。** `hub/core_tools.py::bound_batch_response` 的 `size()` 量的是整个 JSON-RPC：`content[0].text`（JSON 里再套一层 JSON，转义两次）加 `structuredContent`。于是 49,152 字节预算里，正文实际只能占不到一半；超限后 `lines[:len(lines)//2]` 直接折半，又丢掉一截本来放得下的内容。

| 文件 | 正文字节 | 现状整包字节 | 现状一次能回 | 折半实际回 | 正文只走一份时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| shared/unified_contracts.py | 38,663 | 80,561 | 479 行 / 23,422 | **387 行 / 19,969（线上实测）** | 全部 774 行 |
| docs/MCP_PLUGIN_REVIEW_2026-11-09.md | 31,222 | 64,634 | 310/397 行 | — | 全部 |
| hub/runtime.py | 124,193 | 262,975 | 440 行 / 23,250 | — | 835 行 / 47,149 |
| hub/unified_tools.py | 12,448 | 26,501 | 全部 | — | 全部 |

**浪费。** 20KB 以上的文件一律要多读一轮；每多一轮就多一次调用参数、一个信封和一次模型往返。若宿主把两份都送进模型，正文还会翻倍（未验证）。JSON 字符串转义本身只多 2–6% 字符，不是主因。

**建议。**

1. 统一目录默认用已有的 `text=compact`（头部 JSON + 原文正文），并新增 `mirror=meta`：`structuredContent` 只留元数据，正文只走 `content[0].text` 一份。统一工具没有发布 `outputSchema`，元数据镜像不违反 MCP 约定。保留 `?text=json&mirror=full` 作为回退。
2. 预算裁剪由“折半”改成“二分找最大可容纳行数”。

**建议代码。**

`hub/mcp.py`（第 134 行附近，示意）：

```python
text_mode = request.query_params.get('text', 'compact' if profile == 'unified' else 'json')
mirror = request.query_params.get('mirror', 'meta' if profile == 'unified' else 'full')
if mirror not in {'full', 'meta'}:
    return failure(identifier, -32602, 'mirror must be full or meta', 400)
```

`hub/core_tools.py::result`，替换末尾的 compact 分支（新增形参 `mirror='full'`）：

```python
    body_key = None
    if text_mode == 'compact' and not rendered.get('isError') and not public.get('pending'):
        action = getattr(unified, 'action', '')
        if (name == 'read' or action == 'file') and isinstance(public.get('content'), str) and 'sha256' in public:
            body_key = 'content'
        elif (name == 'exec' or action == 'exec') and 'exit_code' in public and isinstance(public.get('output'), str):
            body_key = 'output'
    if body_key:
        header = {key: item for key, item in public.items() if key != body_key}
        text = json.dumps(header, ensure_ascii=False, separators=(',', ':')) + '\n' + public[body_key]
        if mirror == 'meta':
            # 正文只走 content[0].text；镜像只留可机读的元数据。
            rendered['structuredContent'] = {**header, body_key + '_in': 'text'}
    rendered['content'][0]['text'] = text
```

`hub/core_tools.py`，替换折半（`mirror=meta` 时裁剪对象要改成文本正文，逻辑相同）：

```python
def _fit_prefix(count, measure, budget):
    """最大整行前缀：measure(n) 返回保留前 n 行时的最终字节数。绝不切行。"""
    low, high = 0, count
    while low < high:
        middle = (low + high + 1) // 2
        if measure(middle) <= budget:
            low = middle
        else:
            high = middle - 1
    return low

        # bound_batch_response 内，原 `kept = lines[:len(lines) // 2]` 处：
        lines = item['content'].splitlines(keepends=True)

        def measure(count):
            item['content'] = ''.join(lines[:count])
            rendered['content'][0]['text'] = json.dumps(public, ensure_ascii=False, separators=(',', ':'))
            return size()

        fit = _fit_prefix(len(lines), measure, READ_BATCH_PAYLOAD_BYTES)
        # 多文件批读时仍按“最大者至少砍半”收敛；单文件一步到位。
        kept = lines[:max(fit, len(lines) // 2)]
```

**验收。** `shared/unified_contracts.py` 一次读完；`next_offset` / `end_line` / SHA 不变；`?verbosity=full` 与 `?mirror=full` 行为不变；批读 8 项仍收敛；逐个宿主确认只有 `content` 时模型仍能读到正文后再改默认值。

### C02 · 内联等待路径把调度信封原样交给模型 · P0

**证据。** 同一个 `query file`，Agent 直返时是 `{path, sha256, content, ...}`；一旦进入 `_wait`（本次 `（操作编号略）`，耗时 15.1s）就变成 `{"operations":[{id, project_id, tool, args_summary, state, result:{ok,data:{…}}, output:"", created, updated, attempts, next_attempt, deadline, accepted_at, cancel_requested, output_seq, transport_error, last_operation_verified_at, last_output_at, output_chars, output_truncated, operation_id, pending, next, retry_after_seconds, elapsed_seconds}]}`。去掉正文后信封 921 字符，直返形态 242 字符。`present_unified` 目前只给 `exec` 解包、给 `job` 瘦身，其他读操作漏掉了。

**浪费。** 每次多 679 字符；更重要的是同一动作两种结构，`id` 与 `operation_id` 还重复。

**建议代码**（`shared/unified_contracts.py`，已独立运行）：

```python
_UNWRAP_ACTIONS = frozenset({'file', 'tree', 'search', 'symbols', 'history', 'changes'})


def _unwrap_waited(result, plan):
    """等待后已成功的单个只读操作，按直返形态呈现。存储的回执不变。"""
    rows = result.get('operations') if isinstance(result, dict) else None
    if plan is None or plan.action not in _UNWRAP_ACTIONS:
        return result
    if not (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)):
        return result
    row = rows[0]
    nested = row.get('result')
    data = nested.get('data') if isinstance(nested, dict) else None
    if (row.get('pending') or row.get('error') or row.get('state') != 'succeeded'
            or not isinstance(data, dict) or nested.get('ok') is not True or row.get('output')):
        return result          # 未完成、失败、不确定：保留完整回执
    return {'operation_id': row.get('operation_id') or row.get('id'), **data}
```

在 `present_unified` 里，`elif plan and plan.action == 'job':` 分支之后加：

```python
    elif lean:
        result = _unwrap_waited(result, plan)
```

**验收。** 人为延迟 Agent 使读操作进入等待，回包与直返逐键一致；pending / 失败 / 未确认仍返回原信封和 `query job` 恢复请求。先放开白名单内动作，`project open` 等走过专门展示函数的动作确认嵌套数据已投影后再加入。

### C03 · 首次 project open 仍有 3/4 是可折叠的样板 · P1

**证据（本次实测 5,530 字符）。**

| 片段 | 现状 | 建议后 | 说明 |
| --- | ---: | ---: | --- |
| `context.root_entries` | 1,531 | 506 | 39 个 `{"path":…,"type":…}`，改成与 tree 相同的行格式 |
| `context.documents` | 1,137 | 215 | 8 条各带 64 位 SHA 和 `"next":"query file"`；读文件时本来就会返回 SHA |
| `capability_index` | 653 | 0 | 5 个键全是动作枚举里已有的动作，值全是 help 调用；工具说明已写明 help 用法 |
| `knowledge` | 358 | 63 | 64 位 revision、浮点时间、未启用的 maintenance、help 调用 |
| `runtime` | 288 | 29 | 两个 64 位哈希；改成 `mode@ref前12位` |
| `instruction_index` | 115 | 37 | 三条 absent 合并成列表（保留“明确不存在”的语义） |
| 其余 | 1,448 | 457 | 空数组、重复的 `truncated`、`budget`、`next` 句子、版本号、重复的 project/root |
| **合计** | **5,530** | **1,307** | **−76.4%** |

带 `context_id` 重开或 baseline 时 `context` 已经省略，但 `runtime`、`knowledge`、`next`、baseline 的 206 字符 `exclusions` 句子每次仍在（本次 baseline 回包约 1.9K），同一函数一并处理。

**建议代码**（`shared/unified_contracts.py`，已独立运行，输出即上表“建议后”）：

```python
def _line(path):
    return json.dumps(path, ensure_ascii=False) if any(ord(c) < 32 for c in path) or path.startswith('"') else path


def _lean_open(value):
    """minimal 打开回包的展示折叠；完整内容仍可用 ?verbosity=full 取回。"""
    out = {'context_id': value['context_id']}
    if value.get('context_unchanged'):
        out['context_unchanged'] = True
    workspace = value.get('workspace') or {}
    out['workspace'] = {key: workspace[key] for key in ('project', 'mode', 'granted_scopes') if key in workspace}
    if not workspace.get('allow_tasks', True):
        out['workspace']['allow_tasks'] = False
    runtime = value.get('runtime') or {}
    if runtime.get('runtime_ref'):
        out['runtime'] = f"{runtime.get('mode', '')}@{runtime['runtime_ref'][:12]}"
        if runtime.get('isolation') not in (None, 'none'):
            out['runtime_isolation'] = runtime['isolation']
    git = value.get('git')
    if isinstance(git, dict):
        out['git'] = {key: item for key, item in git.items()
                      if key != 'available' and not (key in {'ahead', 'behind'} and item == 0)}
        if isinstance(out['git'].get('head'), str):
            out['git']['head'] = out['git']['head'][:12]
    knowledge = value.get('knowledge')
    if isinstance(knowledge, dict):
        out['knowledge'] = {key: knowledge[key] for key in ('state', 'entries_count', 'scan_scope') if key in knowledge}
        state = (knowledge.get('maintenance') or {}).get('state')
        if state not in (None, 'watching', 'refreshing', 'submitting', 'building', 'initializing'):
            out['knowledge']['maintenance'] = state        # 未启用或异常才显示，见 8.3 的 K3
            if knowledge.get('maintenance_request'):
                out['knowledge']['maintenance_request'] = knowledge['maintenance_request']
    context = value.get('context')
    if isinstance(context, dict):
        ctx = {}
        if context.get('documents'):
            ctx['documents'] = '\n'.join(f"{_line(row['path'])}\t{row.get('bytes', '')}" for row in context['documents'])
        if context.get('remaining_documents_count'):
            ctx['more_documents'] = (str(context['remaining_documents_count'])
                                     + ('+' if context.get('remaining_documents_truncated') else ''))
        rules = context.get('instruction_index') or []
        present = [row for row in rules if row.get('state') != 'absent']
        absent = [row['path'] for row in rules if row.get('state') == 'absent']
        if present:
            ctx['rules'] = present            # 存在的规则文件保留全部字段（含 must_read_before_edit）
        if absent:
            ctx['rules_absent'] = absent
        for key in ('skills', 'codex_skills', 'warnings'):
            if context.get(key):
                ctx[key] = context[key]
        if context.get('root_entries'):
            ctx['root'] = '\n'.join(_line(row['path']) + ('/' if row.get('type') == 'directory' else '')
                                    for row in context['root_entries'])
            if context.get('truncated'):
                ctx['root_truncated'] = True
        out['context'] = ctx
    coverage = value.get('baseline_coverage')
    if isinstance(coverage, dict):
        if coverage.get('complete') and not coverage.get('truncated'):
            coverage = {key: item for key, item in coverage.items()
                        if key not in {'exclusions', 'max_files', 'max_bytes', 'skipped', 'truncated'}}
        out['baseline_coverage'] = coverage
    for key in ('baseline_ref', 'baseline_expires_at', 'context_refresh_required', 'compatibility', 'error'):
        if key in value:
            out[key] = value[key]
    return out
```

接入点：`present_unified` 的 `if lean:` 里，`plan.action in {'project', 'baseline'} and plan.view == 'open'` 且 `context_detail == 'minimal'` 时调用。

**实施前要核对。** `runtime_ref` / `revision` / Git 完整 head 是否被任何**输入参数**引用；若有，对应字段保留完整值。`context_id` 是输入参数，保持完整。表中的 1,307 按“`knowledge.maintenance` 只在启用时显示”的写法测得；按上面代码（未启用也显示状态词）本样本多约 25 字符。

### C04 · 搜索回包：重复键和固定尾巴比命中正文还大 · P1

**证据。** 40 条命中、14 个文件：6,992 字符，命中正文 2,753。每条都带 `"line_truncated":false`（23 字符 × 40），同一文件的 `path` 重复 7 次；尾部固定带约 230 字符的 `limits` 句子、`skipped_reasons:{}`、`scan_error:null`、`skipped_files:0`、与 `truncated` 同义的中文 `hint`。3 条命中的小搜索约 940 字符，命中正文约 240。

**建议代码**（`shared/mcp_presentation.py::token_view` 内新增分支，已独立运行，6,992 → 3,494，含去掉行首缩进）：

```python
    if (name in {'workspace', 'project_query'} and arguments.get('operation') == 'search'
            and arguments.get('options', {}).get('format') != 'structured'
            and isinstance(result.get('matches'), list)):
        groups = {}
        for match in result['matches']:
            path = match.get('path', '')
            if any(ord(char) < 32 for char in path):
                path = json.dumps(path, ensure_ascii=False)
            groups.setdefault(path, []).append(
                f"{match.get('line')}:{match.get('text', '').strip()}" + (' …' if match.get('line_truncated') else ''))
        result['matches'] = '\n'.join(path + '\n' + '\n'.join(rows) for path, rows in groups.items())
        result['matches_format'] = 'path_line_then_line_colon_text'
        if result.get('scan_status') in {'matches', 'page_limited', 'no_matches'}:
            result.pop('limits', None)        # 只有真的撞到扫描上限才需要这段说明
        for key in ('hint', 'scan_error', 'skipped_reasons', 'skipped_files'):
            if not result.get(key):
                result.pop(key, None)
```

`scan_status` 的取值集合以 `agent/searches.py` 实际枚举为准；保留 `args.format=structured` 取回逐条对象。**验收**：零扫描、零命中、分页上限、扫描预算四种状态仍可区分；`next_offset` 不变。

### C05 · 目录树：不遵守 .gitignore、没有单目录上限 · P1

**证据。** `agent/filesystem.py::tree` 走 `self.walk`；search 和 knowledge 走 `git_candidates`（`scan_scope: git_index`）。本项目 `dist/` 被 `.gitignore` 忽略、Git 跟踪 0 个文件，但根目录 tree 首屏 300 行里有 191 行是它（8,740 / 11,764 字符，磁盘重放）。根目录两层共 1,432 行，要翻 5 页才看完。另有多处 `.DS_Store`，以及每次首屏固定的约 150 字符 `exclusions` 句子。

**对照（磁盘重放）。**

| 方案 | 行数 | 字符 | 覆盖 |
| --- | ---: | ---: | --- |
| 现状首屏 | 300 | 11,764 | 根目录约 1/5 |
| Git 可见 + 缩进 + 每目录最多 25 行 | 240 | 5,771 | 整个根目录，一页 |
| Git 可见 + 缩进 + 每目录最多 40 行 | 345 | 8,701 | 整个根目录 |

缩进格式单独的效果：`hub/` 195 行，5,600 → 4,327 字符（−23%）。

**建议代码 1：Agent 侧**（`agent/filesystem.py::tree`，示意；`git_candidates` 的返回结构以实际为准）：

```python
        # 默认与 search/knowledge 一致：只列已跟踪 + 未被忽略的未跟踪文件。scope=all 才列全部。
        visible = None
        if args.get("scope", "git") == "git":
            candidates = self.git_candidates(root, start)       # None：不是 Git 项目或枚举失败 → 回退 walk
            if candidates is not None:
                visible = set()
                for rel in candidates:
                    parts = rel.split("/")
                    visible.update("/".join(parts[:end]) for end in range(1, len(parts) + 1))
        per_dir, shown, omitted, index = args.get("per_dir", 40), {}, {}, -1
        ...
            for p, rel, st in self.walk(root, start, args["depth"], deadline=time.monotonic() + 8):
                if visible is not None and rel not in visible:
                    continue                                   # 不计入分页序号
                parent = rel.rpartition("/")[0]
                shown[parent] = shown.get(parent, 0) + 1
                if shown[parent] > per_dir:
                    omitted[parent] = omitted.get(parent, 0) + 1
                    continue
                index += 1
                if index < args["offset"]:
                    continue
                ...
        return {..., "scan_scope": "git_index" if visible is not None else "filesystem_walk",
                "omitted": omitted or None}                    # {"dist": 198} → 模型按需 path=dist 展开
```

更省的做法是让 `walk` 直接不下钻被忽略的目录。`exclusions` 句子只在 `scope=all` 或 `scan_error` 时返回。

**建议代码 2：展示侧缩进**（`token_view` 的 compact tree 分支，替换 `rows.append(...)` 一段）：

```python
            stack = []                          # 当前正在列出子项的目录链
            for entry in result['entries']:
                path = entry.get('path', '')
                if prefix and path.startswith(prefix):
                    path = path[len(prefix):]
                parent, _, leaf = path.rpartition('/')
                while stack and stack[-1] != parent:
                    stack.pop()
                if not parent:
                    label = leaf
                elif stack:
                    label = ' ' * len(stack) + leaf    # 父目录就在上方：只写名字
                else:
                    label = path                       # 翻页后父目录不在本页：写完整相对路径
                if any(ord(char) < 32 for char in label) or label.lstrip(' ').startswith('"'):
                    label = json.dumps(label, ensure_ascii=False)
                is_dir = entry.get('type') == 'directory'
                rows.append(label + ('/' if is_dir else '\t' + str(entry.get('size', ''))))
                if is_dir:
                    stack.append(path)
            result['entries_format'] = 'indent_name_slash_or_tab_size'
```

**验收。** 被忽略目录默认不出现，`scope=all` 可见；分页序号连续；翻页首行不依赖上一页；文件名含空格、制表符、换行时边界不丢。Agent 侧改动需要随 Agent 版本发布，旧 Agent 回退为现状。

### C06 · 项目列表：28 行各重复三个相同字段和一个 32 位 id · P2

**证据（按真实字段重建）。** 3,200 字符。28 行里 `mode:"write"`、`allow_tasks:true` 全部相同，`online` 只有 3 个例外；模型按别名打开项目，用不到 id。尾部 `view_version`、`mcp_view_version`、`query:""`、`offset:0`、`next_offset:null` 也无决策价值。

**建议代码**（`shared/unified_contracts.py`，替换 `_project_list` 的 lean 输出，已独立运行，3,200 → 416，−87%）：

```python
def _compact_projects(value):
    rows, keys = value['projects'], ('mode', 'allow_tasks', 'online')
    common = {}
    for key in keys:
        counts = {}
        for row in rows:
            token = json.dumps(row.get(key))
            counts[token] = counts.get(token, 0) + 1
        common[key] = json.loads(max(counts, key=counts.get)) if counts else None
    plain = [row['alias'] for row in rows
             if all(row.get(key) == common[key] for key in keys) and not row.get('description')]
    out = {'defaults': common, 'projects': plain}
    other = {row['alias']: {**{key: row.get(key) for key in keys if row.get(key) != common[key]},
                            **({'description': row['description']} if row.get('description') else {})}
             for row in rows if row['alias'] not in plain}
    if other:
        out['exceptions'] = other
    if value.get('next_offset') is not None:
        out['next_offset'] = value['next_offset']
    out['total'] = value.get('total', len(rows))
    return out
```

输出形如 `{"defaults":{"mode":"write","allow_tasks":true,"online":true},"projects":["示例项目A","示例项目B",…],"exceptions":{"示例项目C":{"online":false},…},"total":28}`。别名重名时该行放进 `exceptions` 并带 id；`args.format=structured` 取回完整行。

### C07 · exec 成功回执 · P2

**证据。** 本次成功命令除 `exit_code` / `output` 外还有 404 字符：两个 64 位哈希的 `runtime`（约 215 字符，每次 exec 都重复 open 里已有的值）、`command_ok:true`、`state:"succeeded"`、`pending:false`、`output_truncated:false`、`target:"agent"`。

**建议代码**（并入 `_present_exec` 成功分支末尾，已独立运行，404 → 约 150）：

```python
        for key in ('state', 'command_ok', 'pending'):       # exit_code == 0 且未 pending 时均为冗余
            result.pop(key, None)
        if result.get('output_truncated') is False:
            result.pop('output_truncated')
        if result.get('target') == 'agent':
            result.pop('target')
        runtime = result.get('runtime')
        if isinstance(runtime, dict) and runtime.get('runtime_ref'):
            result['runtime'] = f"{runtime.get('mode', '')}@{runtime['runtime_ref'][:12]}"
```

仅在 `exit_code == 0`、非 pending、无 error 时生效；失败、超时、取消、未确认保持现状。`operation_id` 和 `output_seq` 保留。

### C08 · 工具目录 schema · P2

**证据。** 两个工具的 `inputSchema` 共 3,489 字符，其中 `file`（`NativeFile.model_json_schema()` 原样，带 `title` / `default` / `anyOf null`）每个 608 字符、共 1,216，占 35%。每个字段还带只对服务端校验有意义的 `maxLength`。当前连接即使看不到 `source_check` / `import`，`file` 也照发。

**建议代码**（`shared/unified_contracts.py`，已独立运行：3,489 → 2,550，−27%；不含 `file` 时 query 763 / act 1,207）：

```python
def _slim_schema(node, *, root=True):
    """去掉只复述服务端校验的关键字。服务端仍按原模型完整校验。"""
    if isinstance(node, list):
        return [_slim_schema(item, root=False) for item in node]
    if not isinstance(node, dict):
        return node
    options = node.get('anyOf')
    if isinstance(options, list):
        real = [item for item in options if item != {'type': 'null'}]
        if len(real) == 1:
            node = {**{key: item for key, item in node.items() if key != 'anyOf'}, **real[0]}
    out = {}
    for key, value in node.items():
        if key in {'title', 'default', 'maxLength'}:
            continue
        if key == 'additionalProperties' and (not root or value is True):
            continue
        if key == 'properties' and isinstance(value, dict):   # 属性名本身可能叫 title/default
            out[key] = {name: _slim_schema(item, root=False) for name, item in value.items()}
        else:
            out[key] = _slim_schema(value, root=False)
    return out


def input_schema(tool, actions):
    names = [name for name in ('view', *TOP_LEVEL[tool], 'wait', 'args')
             if name != 'file' or {'source_check', 'import'} & set(actions)]
    properties = {'action': {'type': 'string', 'enum': list(actions)}}
    properties.update({name: copy.deepcopy(_PROPERTIES[name]) for name in names})
    return _slim_schema({'type': 'object', 'properties': properties,
                         'required': ['action'], 'additionalProperties': False})
```

保留 `maxItems`、`wait` / `timeout_seconds` 的上下限、`target` 与 `expected_sha256` 的 pattern（它们能避免一次参数错误）。`tests/test_unified_tools.py` 的“目录不超过 10,000 字符”继续成立。说明文字（685 / 713 字符）已经较紧，不建议为再省 200 字符冒首次调用失败的风险。

### C09 · 无参 help 把 115 个协作 view 全部列出 · P2

**证据。** `query(action="help")` 回包 3,180 字符，核心动作只占 1,091；其余是协作 view 名单。不做协作的会话每查一次目录多付约 2,000 字符。

**建议代码**（`hub/unified_tools.py::catalog`）：

```python
def catalog(scopes=None, *, collaboration=False):
    tools = directory(scopes, collaboration=collaboration)
    collab = {tool: [action for action in actions if action.startswith('collab.')] for tool, actions in tools.items()}
    core = {tool: {action: views for action, views in actions.items() if not action.startswith('collab.')}
            for tool, actions in tools.items()}
    result = {'tools': core,
              'note': "Choose a view or use the default. query help accepts args.tool/action/view; "
                      "args.detail=schema returns machine schemas. Action-specific fields go in args."}
    if any(collab.values()):
        result['collab'] = {tool: names for tool, names in collab.items() if names}
        result['note'] += " Collaboration views: query help with args.action='collab.<name>'."
    return result
```

定向 help（`args.action="collab.get"`）已经返回 `views`，不需要另加入口。

### C10 · 零散项 · P3

- 值为 `null` / `false` / `[]` / `{}` / `0` 的默认字段：`truncated:false`、`next_offset:null`、`scan_error:null`、`skills:[]`、`warnings:[]`、`hint:null` 等。lean 展示统一省略“等于默认值”的键，在 `UNIFIED_TOOLS.md` 写明“缺省即默认”。
- 时间戳 `1794285686.854903` 这类 6–7 位小数的浮点，lean 展示取整秒。
- `view_version`、`mcp_view_version` 每个回包都带，模型不用。
- 已成功的只读 query 是否保留 32 位 `operation_id`：约 51 字符/次。它对追溯有用（本文第 7 节就在用），建议保留为可选项而不是直接删。
- tree 里的 `.DS_Store`：加入默认排除。

## 3. 建议的落地顺序

| 批次 | 项 | 性质 | 备注 |
| --- | --- | --- | --- |
| 1 | C02、C04、C07、C10 | 纯展示，Hub 侧 | 无宿主依赖，风险最低 |
| 2 | C03、C06、C09 | 纯展示，改变了键结构 | 现有断言 `capability_index` / `projects[].id` 的测试要同步 |
| 3 | C05 | Agent + 展示 | 随 Agent 版本发布，旧 Agent 自动回退 |
| 4 | C01 | 传输形态 | 先在 Claude、ChatGPT 各验证“只有 content 时模型读得到正文”，再改统一目录默认值 |
| 5 | C08 | 目录 | 宿主重扫目录后生效 |
| 并行 | K1、K2、K3（第 8 节） | Hub 侧，地图维护 | 不改授权模型；K4 是操作步骤，随时可做 |
| 并行 | K5（第 8 节） | Hub 侧，默认开启 | 已决定要做（2026-11-10）；与 K1–K3 一起实现，同步改写 `KNOWLEDGE_AUTO_REFRESH.md` 里的两条规则 |

第 1–3 批改变的是回包形状，建议沿用已有的 `view=1|2` 机制新增 `view=3`，先显式开启、用 `scripts/benchmark_mcp_workflow.py` 的五步流程（建议加一步“根目录 tree”和一个 >20KB 的文件读取）做前后对照，再设为统一目录默认。`?verbosity=full` 始终返回未折叠的回执。

## 4. 不建议做的事

- **不合并 `query` / `act`。** 只读提示是按工具声明的，合并会让宿主要么每次读都审批，要么写也自动放行。
- **不删 SHA、幂等键、`context_id`、`output_seq`。** 它们防的是覆盖写、重复执行和重复日志，省掉后出一次错的代价远高于这些字符。
- **不做自动摘要式截断。** 读文件、命令输出只做有界分页和明确标记的截断，不替模型改写正文。
- **不引入 gzip / base64 之类的编码。** 对模型而言只会更贵。
- **不为省字符去掉失败、pending、未确认状态下的诊断字段。** 本文所有折叠都限定在“已成功且终态”。

## 5. 未验证项

- 真实 token 与计费：未测。本文所有百分比都是字符口径。
- 各宿主对 `content` 与 `structuredContent` 的取用方式；C01 的默认值切换以此为前提。
- C05 的数字是磁盘重放，不是改完后的实际回包；`git_candidates` 的返回结构、`walk` 的遍历顺序以源码为准。
- `runtime_ref`、`revision`、完整 Git head 是否被输入参数引用（C03 / C07 缩短前要查）。
- 文中函数只在测量脚本里对真实回包单独运行过，没有接入主流程，也没有运行项目测试套件。
- 第 8 节的 K1–K5 代码按源码结构编写，没有运行；本次也没有为任何项目开启自动维护。

## 6. 本次没有动的东西

只新增了本文件。创建前已对该路径做基线（`absent`）。工作区原有 16 个未提交改动未触碰。

## 7. 测量证据

本节涉及授权连接上的实测回执。公开设计文档保留测量口径与结果，原始回执编号保留在本地记录中。

## 8. 项目地图（knowledge）自动维护：现状实现与补强

> 本节回答“通过 MCP 干活时，地图能不能每次自动更新”。结论：机制已有，但默认不开、按轮询而不是按写操作触发、忙时让路、异常后静默停用。K1–K5 的代码按源码结构编写，未运行。
>
> 决定（2026-11-10）：K5 要做，而且自动做——默认开启，不需要任何人记得去开。

### 8.1 现状实现（HEAD `46dd9224ac6b`）

**登记。** 只有显式生成会登记。`hub/runtime.py` 在操作入队处调用 `knowledge_maintenance.admitted(...)`，它只处理 `project_knowledge_build`。`auto_refresh` 为 true 且调用方带 `grant_id` 时，在 `knowledge_maintenance` 表按 `digest([project id, device_id, root, workspace_id])` 写入一行，保存加密的原主体快照、代次 `generation` 和 `build_options`，`next_at = now + 120`。`auto_refresh: false` 把已有行置为停用，`null` 不动登记。面板、管理员等无 grant 主体不登记，回执标 `unsupported_subject`。

**调度。** `KnowledgeMaintenance.run` 每 15 秒 `tick` 一次，`due()` 取 `enabled=1 AND next_at<=now` 的前 4 行。上限：全局 128 行，单设备 32 张启用地图。

**每一轮 `prepare` 依次做的事。**

1. 解密主体，要求有 grant、用户仍存在，清掉 admin 位后 `refresh_principal` 取当前权限。
2. 用 `incremental: true`、`auto_refresh: null` 组装参数，走完整的 `runtime._invoke('project_knowledge_build', …)` 校验。项目根目录或设备变了就停用（`KNOWLEDGE_MAPPING_CHANGED`）。
3. 收割上一轮操作：
   - 仍在运行：保持原状态，再等 120 秒；
   - `needs_review`、`interrupted` 或找不到：标 `needs_review` 并停用；
   - 失败：标 `backoff`，延迟 `min(3600, 120 × 2^min(失败次数+1, 5))` 秒；首次生成失败则标 `initial_build_failed` 并停用；
   - 成功：标 `watching`，记 `last_success`；结果是 `building` 时保存 cursor，延迟 0 继续下一批。
4. 设备离线标 `offline`；设备上有任何排队或运行中的操作标 `deferred_busy`。两者都再等 120 秒。
5. 写入 `pending_key`、状态 `submitting`、`next_at = now + 120`，然后 `dispatch(..., background=True, maintenance=guard)`。入队时 `guard` 复核代次和幂等键，状态变 `refreshing`。

所以稳态下一行大部分时间停在 `refreshing`：上一轮操作早已完成，但要等到 `next_at` 才被收割，并在同一次 `prepare` 里提交下一轮。

**Agent 侧。** `agent/knowledge_refresh.build`：有内存缓存、地图版本一致、距上次完整读取不到 900 秒时走增量。设备号、inode、大小、mtime_ns、ctime_ns、mode、硬链接数和缓存的 SHA / 结构版本全部一致才复用条目，否则重读并重新提取。缓存只在 Agent 内存，最多 32 张，重启或淘汰后完整读取。没有地图也没有 cursor 时，增量请求报 `KNOWLEDGE_MISSING`。

**状态出口。** 生成和查询回执带 `auto_refresh`（`state`、`next_check_at`、`last_success_at`、`last_error`、`operation_id`）。`project open` 在地图 `ready` 时只带 `maintenance: {state, enabled}`（`hub/project_views.py`）。

**不变的保证。** 查询和自动上下文逐条核对源文件 SHA，过期摘要不返回。地图落后时的表现是“缺条目”，不是“给错内容”。

### 8.2 为什么做不到“每次干活都更新”

| 缺口 | 源码位置 | 后果 |
| --- | --- | --- |
| G1 默认不开 | `admitted` 只认显式生成；旧地图、打开项目、只读查询都不登记 | 本项目当前就是 `disabled`，地图约 16.5 小时没更新（打开回包实测） |
| G2 不按写操作触发 | 操作终态落库处（`hub/runtime.py` 约 1628–1655 行）没有地图钩子 | `edit` / `exec` 之后最长要等一个 120 秒周期才提交刷新 |
| G3 忙时让路 | `prepare` 里 `deferred_busy` 用默认延迟 120 秒 | 连续干活时反复顺延，最需要刷新的时候刷不了 |
| G4 异常后静默停用 | `paused`、`needs_review`、`initial_build_failed` 都把 `enabled` 置 0 | 之后一直不更新；`open` 只显示一个状态词，不告诉模型怎么恢复 |
| G5 逐项目手动开启 | 28 个项目要各生成一次；单设备上限 32；`workspace_id` 不同算不同地图 | 容易漏开 |

### 8.3 补强实现

**K1 · 写操作结束后把下一次检查提前（解决 G2）。** 只提前已有且启用的登记；不新建，不改授权，退避中的不动。

`hub/knowledge_maintenance.py`：

```python
NUDGE_DELAY = 10        # 写操作结束后多久检查
MIN_GAP = 30            # 两次维护提交的最小间隔；连续编辑合并成一次
BUSY_RETRY = 20
MUTATING_TOOLS = frozenset({'write', 'edit', 'apply_patch', 'fs_write', 'fs_edit',
                            'exec', 'shell_exec', 'tasks_run'})   # 以 runtime 实际落库的工具名为准


class KnowledgeMaintenance:
    ...
    def nudge(self, project_id, device_id, tool):
        """项目文件可能已变：让已有登记尽快检查。从不创建登记，从不把检查延后。"""
        if tool not in MUTATING_TOOLS or not project_id or not self.exists():
            return
        now = time.time()
        self.store.execute('UPDATE knowledge_maintenance SET last_write=? WHERE project_id=? AND device_id=?',
                           (now, project_id, device_id))      # K5 的空闲判断和挤出顺序用
        due = now + NUDGE_DELAY
        self.store.execute(
            '''UPDATE knowledge_maintenance SET next_at=MAX(?, updated + ?)
               WHERE project_id=? AND device_id=? AND enabled=1
                 AND state IN ('watching','refreshing','deferred_busy','offline')
                 AND next_at>MAX(?, updated + ?)''',
            (due, MIN_GAP, project_id, device_id, due, MIN_GAP))
```

`hub/runtime.py`，操作终态落库的函数里，`notify_operation` 调用之后：

```python
        # 成功、失败、待核实都可能已经改了文件；提前检查没有副作用。
        if op.get('project_id') and op['tool'] != 'project_knowledge_build':
            try:
                self.knowledge_maintenance.nudge(op['project_id'], op['device_id'], op['tool'])
            except Exception:
                pass        # 辅助路径：不能影响原操作的终态提交
```

`refreshing` 必须在白名单里，因为稳态下行就停在这个状态。提前后的那次 `prepare` 会先收割已完成的上一轮，再在同一次调用里提交新的增量刷新。

**K2 · 忙和在途时用短间隔重试（解决 G3，并保证 K1 不被顺延回 120 秒）。** `prepare` 内两处，把默认延迟改成 `BUSY_RETRY`：

```python
                if operation['state'] in ACTIVE:
                    self.update(row, row['state'], delay=BUSY_RETRY)      # 原为默认 120 秒
                    return None
...
            # “设备上有排队或运行中的操作”那个判断的分支里：
                self.update(row, 'deferred_busy', delay=BUSY_RETRY)       # 原为默认 120 秒
                return None
```

效果：设备空闲时，写操作结束后约 10–25 秒提交增量刷新（10 秒延迟加 15 秒循环粒度）；撞上忙时每 20 秒重试一次，活一停就刷新。代价是忙时每 20 秒多一次单行查询，`due()` 每轮仍只取 4 行。

**K3 · 停用和异常时在 open 里给出可执行的下一步（解决 G4，辅助 G1）。** `hub/project_views.py`，替换 `ready` 分支：

```python
        if knowledge.get('state') == 'ready':
            maintenance = runtime.knowledge_maintenance.status(project, args)
            knowledge['maintenance'] = {key: maintenance[key] for key in ('state', 'enabled')}
            if maintenance['state'] not in {'watching', 'refreshing', 'submitting', 'building', 'initializing'}:
                for key in ('last_error', 'last_success_at'):
                    if maintenance.get(key):
                        knowledge['maintenance'][key] = maintenance[key]
                from hub.roles import role_project_scopes
                if project['mode'] == 'write' and 'write' in role_project_scopes(runtime.store, principal, project['id']):
                    # *_request 键会被 _rewrite_hints 改写成 act knowledge 的 help 调用，并按当前目录过滤。
                    knowledge['maintenance_request'] = 'project_knowledge_build'
            knowledge['next'] = 'project_knowledge_query'
```

`shared/mcp_presentation.py::_open` 里 `_pick(value['knowledge'], {...})` 的白名单要加上 `maintenance_request`。C03 的 `_lean_open` 已按这个约定写：健康状态不显示，其余状态显示状态词和 `maintenance_request`。不建议为此再加一句常驻的服务端说明，提示只在需要时出现在 open 里，不占每个会话的固定上下文。

**K4 · 一次性把现有项目都开起来（解决 G5）。** 用带 grant 的连接对每个在线项目各调用一次，幂等键各不相同：

```
act  action=knowledge  project=<别名>  idempotency_key=knowledge-on-<别名>-20261110
```

`auto_refresh` 默认 true。回执里 `auto_refresh.state` 应为 `initializing`，随后变成 `watching` 或 `refreshing`。出现 `auto_refresh_request.state: unsupported_subject` 说明当前身份没有 grant，要换连接再做。离线项目上线后再开。K5 上线后，这一步只用于想在第一次写操作之前就有地图的项目。

**K5 · 写操作时自动登记并自动建图（已决定要做，默认开启）。** 目标：只要有人带写权限通过 MCP 在一个项目上干活，这个项目就有一张在维护的地图，以前从没建过图的项目也一样。

**规则。**

- 触发点是写操作入队（`edit` / `write` / `exec` 等）。调用方必须是带 grant 的身份，并且对该项目有 read + write。只读查询、打开项目、升级仍然不登记。
- 没有登记行：登记，第一轮做一次完整生成（内部标记 `bootstrap`），之后走增量。
- 已在维护：不做任何事，提前检查由 K1 负责。
- 用户显式关闭过（`auto_refresh: false`，状态 `disabled`）：尊重，不自动恢复。
- `needs_review`（上一轮结果未确认）：不自动恢复，仍由 K3 提示处理。这是“不用新幂等键重放未确认操作”那条规则的延续。
- `paused`、`initial_build_failed`：停用满 15 分钟后，下一次写操作用当前身份重新登记。
- 登记前先用后台每轮相同的 `_invoke` 完整校验一次；房间范围、角色或项目策略不允许生成地图时不登记。
- 登记失败绝不影响原写操作。
- 达到上限（全局 128、单设备 32）时，挤掉最久没有写操作的一张**自动登记**的地图；显式登记的从不挤掉，被挤掉的在下一次写操作时重新登记。
- 自动登记的地图 24 小时没有写操作后，轮询间隔从 120 秒放宽到 30 分钟；下一次写操作由 K1 立即拉回。

**会改掉的现有规则。** `KNOWLEDGE_AUTO_REFRESH.md` 里有两句要同步改写：“旧版本已经存在的地图不会因升级、打开项目或只读查询而自动登记，需要再显式生成一次”，以及“不能为新目录或被替换的根目录隐式创建地图”。新规则的依据：触发者此刻正以写权限操作这个项目，登记绑定的就是这个 grant，之后每一轮照旧重新核对权限；读操作依然没有任何副作用。

`hub/knowledge_maintenance.py`，新增常量、两列和四个方法：

```python
AUTO_ENROLL_COOLDOWN = 900      # 系统停用后多久允许自动恢复
IDLE_AFTER = 86400              # 自动登记的地图多久没有写操作算空闲
IDLE_INTERVAL = 1800            # 空闲时的轮询间隔


class KnowledgeMaintenance:
    def __init__(self, runtime):
        ...
        # 默认开启。接入项目自己的设置机制后，这里改成读设置项。
        self.auto_enroll = os.environ.get('CODEPIER_KNOWLEDGE_AUTO_ENROLL', '1') != '0'

    def migrate(self):
        ...                                         # 现有建表与 build_options / cursor 两列不变
        columns = {r['name'] for r in self.store.all('PRAGMA table_info(knowledge_maintenance)')}
        if 'origin' not in columns:
            self.store.execute("ALTER TABLE knowledge_maintenance ADD COLUMN origin TEXT NOT NULL DEFAULT 'explicit'")
        if 'last_write' not in columns:
            self.store.execute('ALTER TABLE knowledge_maintenance ADD COLUMN last_write REAL')

    def _register(self, identifier, project, args, principal, *, enabled, state, operation_id, next_at,
                  origin='explicit', build_options=None):
        """admitted 里现有的 INSERT OR REPLACE 抽成公共函数；admitted 改为调用它。"""
        value = asdict(principal)
        value['scopes'] = sorted(value['scopes'])
        value['token_hash'] = value['session_hash'] = ''
        now = time.time()
        self.store.execute('''INSERT OR REPLACE INTO knowledge_maintenance
            (id,project_id,device_id,root,workspace,principal,generation,enabled,state,max_files,
             operation_id,pending_key,next_at,failures,last_error,last_success,updated,build_options,cursor,
             origin,last_write)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (identifier, project['id'], project['device_id'], project['root'], args.get('workspace_id', ''),
             self.store.encrypt(json.dumps(value)), uuid.uuid4().hex, int(enabled), state, args.get('max_files', 0),
             operation_id, '', next_at, 0, '', None, now, json.dumps(build_options or {}), args.get('cursor', ''),
             origin, now if origin == 'auto' else None))

    def _make_room(self, device_id, identifier):
        """上限内返回 True。满了就删掉一张自动登记的地图：先删已停用的，再删最久没有写操作的。"""
        total = self.store.one('SELECT count(*) AS n FROM knowledge_maintenance')['n']
        on_device = self.store.one(
            'SELECT count(*) AS n FROM knowledge_maintenance WHERE device_id=? AND enabled=1 AND id<>?',
            (device_id, identifier))['n']
        if total < MAX_RECORDS and on_device < MAX_DEVICE_RECORDS:
            return True
        if on_device >= MAX_DEVICE_RECORDS:
            victim = self.store.one(
                '''SELECT id FROM knowledge_maintenance WHERE device_id=? AND origin='auto' AND enabled=1 AND id<>?
                   ORDER BY COALESCE(last_write, 0) LIMIT 1''', (device_id, identifier))
        else:
            victim = self.store.one(
                '''SELECT id FROM knowledge_maintenance WHERE origin='auto' AND id<>?
                   ORDER BY enabled, COALESCE(last_write, 0) LIMIT 1''', (identifier,))
        if not victim:
            return False                             # 全是显式登记：不挤，不登记
        # 未交付的在途刷新会被 guard() 拦下；已交付的可能照常完成。
        self.store.execute('DELETE FROM knowledge_maintenance WHERE id=?', (victim['id'],))
        return True

    def enroll_on_write(self, name, args, project, principal):
        """写操作入队时保证项目有一张在维护的地图。绝不抛错，绝不阻塞原操作。"""
        if (not self.auto_enroll or name not in MUTATING_TOOLS or not principal.grant_id
                or not project.get('id') or not {'read', 'write'} <= set(principal.scopes)):
            return
        try:
            self.migrate()
            identifier, now = key_for(project, args), time.time()
            row = self.store.one('SELECT enabled,state,updated FROM knowledge_maintenance WHERE id=?', (identifier,))
            if row:
                if row['enabled']:
                    return                           # 已在维护
                if (row['state'] not in ('paused', 'initial_build_failed')
                        or now - row['updated'] < AUTO_ENROLL_COOLDOWN):
                    return                           # 用户显式关闭、结果待核实，或刚被停用
            probe = {'project': project['id'], 'auto_refresh': None, 'max_files': 0,
                     'idempotency_key': 'knowledge-auto-' + uuid.uuid4().hex}
            if args.get('workspace_id'):
                probe['workspace_id'] = args['workspace_id']
            # 与后台每轮相同的完整校验；不允许生成地图的身份在这里被拒，不登记。
            self.runtime._invoke('project_knowledge_build', probe, principal)
            if not self._make_room(project['device_id'], identifier):
                return
            self._register(identifier, project, probe, principal, enabled=True, state='initializing',
                           operation_id='', next_at=now + NUDGE_DELAY, origin='auto',
                           build_options={'bootstrap': True})
        except DevError:
            return
        except Exception as exc:
            LOGGER.warning('Knowledge auto-enroll skipped (%s)', type(exc).__name__)

    def interval(self, row):
        if row.get('origin') == 'auto' and time.time() - (row.get('last_write') or 0) > IDLE_AFTER:
            return IDLE_INTERVAL
        return INTERVAL
```

`update()` 的 `delay` 默认值改为 `None`，函数内取 `self.interval(row) if delay is None else delay`。

`prepare` 里三处改动（首次完整生成，之后增量）：

```python
            options = json.loads(row['build_options'])
            bootstrap = bool(options.pop('bootstrap', False))      # 内部标记，不能传给生成参数
            args = {**options, 'project': row['project_id'], 'max_files': row['max_files'],
                    'auto_refresh': None, 'incremental': not bootstrap,
                    'cursor': row['cursor'],
                    'idempotency_key': row['pending_key'] or 'knowledge-auto-' + uuid.uuid4().hex}
...
                else:                                              # 上一轮完整成功的分支
                    args['cursor'] = ''
                    args['incremental'] = True                     # 首次完整生成已完成
                    args.pop('entries', None)
                    build_options = json.loads(row['build_options'])
                    build_options.pop('entries', None)
                    build_options.pop('bootstrap', None)
                    ...
...
            # 提交前写 next_at 的那一句：
            self.store.execute('UPDATE knowledge_maintenance SET pending_key=?,state=?,next_at=? WHERE id=? AND generation=?',
                               (args['idempotency_key'], 'submitting', time.time() + self.interval(row),
                                row['id'], row['generation']))
```

首次生成失败时行已是 `refreshing`，走的是 `backoff` 而不是 `initial_build_failed`，`bootstrap` 标记保留，退避后重试完整生成。

`hub/runtime.py` 入队处，紧跟现有的 `admitted(...)`：

```python
                self.knowledge_maintenance.admitted(name, args, project, principal, id, maintenance)
                if maintenance is None:                # 后台维护自己提交的操作不触发登记
                    self.knowledge_maintenance.enroll_on_write(name, args, project, principal)
```

这里在入队事务内执行。如果 store 的事务会被一条失败语句污染，就改成用 `self.store.after_commit(...)` 在提交后登记。

**代价与边界。**

- 每个项目第一次被写时会触发一次完整读取。它按批次预算分批，设备忙时让路（K2 的 20 秒重试），不调用模型，不执行项目代码。
- 写过的项目都会有地图并持续轮询。空闲放宽（30 分钟）和上限挤出控制长期开销；这期间只有工具外的修改（IDE、Git）会晚一些被发现。
- 只有只读权限的人干活不会触发；他们用的是别人写操作时登记的那张地图。
- 地图仍然不是即时的：K1 + K2 之后是写操作结束后十几到几十秒；查询时照旧按 SHA 隐藏过期条目。

### 8.4 验收

- 已登记项目上执行一次 `act edit`：空闲设备 25 秒内出现新的 `project_knowledge_build` 后台操作，`query knowledge` 能查到改动后的结构。未登记项目上同样操作不产生任何登记或后台操作。
- 10 秒内连续 5 次编辑只提交 1 次刷新；`backoff`、`paused`、`needs_review` 的行不被 K1 提前。
- 设备持续有操作时，最后一个操作结束后 35 秒内完成提交。
- 停用、`paused`、`needs_review` 的项目，`open` 回包带 `maintenance_request`；只读凭据不带。
- 原有保证不变：每轮重新核对 grant 和权限；撤销 grant 后下一轮停用；代次与幂等键围栏仍阻止过期提交；只读查询不创建任何登记或表。
- K5：带写权限的 grant 身份在**从未建过图**的项目上执行一次 `act edit`，原操作正常返回；随后出现一次完整生成的后台操作，完成后 `open` 显示 `knowledge.state: ready`，之后的编辑走增量。
- K5：只读凭据、面板或管理员身份、只读查询、打开项目都不产生登记。
- K5：显式 `auto_refresh: false` 之后的写操作不恢复登记；`needs_review` 不自动恢复；`paused` 满 15 分钟后的下一次写操作恢复。
- K5：登记或校验抛错时，原写操作的结果不受影响。
- K5：单设备第 33 张地图会挤掉最久没有写操作的一张自动登记地图，显式登记的保留。
- K5：`CODEPIER_KNOWLEDGE_AUTO_ENROLL=0` 时行为与现状完全一致。
