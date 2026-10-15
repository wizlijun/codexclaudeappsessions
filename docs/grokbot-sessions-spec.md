# Grok Bot 会话导出 Spec

- 状态：草案（Draft）
- 日期：2026-10-15
- 适用范围：`codexclaudeappsessions` 中的 `grokbot` vendor（`grokbot_md.py`、`export_sessions.py`、`config.yml`、`runapp.sh`）
- 原则：只读本机数据、离线运行、不上传、不调用任何私有服务端 API

---

## 1. 背景与目标

### 1.1 背景

当前未提交的实现（`grokbot_md.py` + `export_sessions.py::grokbot_tasks()`）假设 Grok Bot 会话保存在 SQLite：

```
<root>/agents/<agentId>/store.db   -> transcript_entries(seq, id, entry JSON)
```

默认搜索根 `~/.grokbot` 与 `~/Library/Application Support/Grok Bot`。实测：

- Mac 上两个目录都**没有** `agents/<id>/store.db`，`grokbot_tasks()` 输出 0 个会话；
- 这些 `store.db` 实际位于 bot 电脑（box）`/home/box/agent-data/agents/<id>/store.db`，且其中 `transcript_entries` 目前**为空**（7 个 bot 中 6 个 0 行，1 个只有 3 条 `event`）——完整历史在服务端，不在本地 SQLite。

真正存在于 Mac 本地、可读的会话数据，是桌面 App 的客户端持久化缓存：
`~/Library/Application Support/Grok Bot/sand-client-persistence/*.blob`（明文 JSON）。

### 1.2 目标

1. 新增数据源：解析 `sand-client-persistence` 中的 `transcript.replicas.<agentId>`，按 bot 导出 Markdown，接入现有增量导出 / 索引流程。
2. 完全离线、只读：不复制、不上传任何文件出本机；不修改 App 数据目录。
3. 由于本地缓存是**滚动窗口（约 200 条）**，引入 append-only 本地归档，使多次运行累积出尽可能完整的历史。
4. 保留 `store.db` 读取作为回退源（`grokbot_data` 指向时）。

---

## 2. 数据源

### 2.1 App

| 项 | 值 |
|---|---|
| 路径 | `/Applications/Grok Bot.app` |
| Bundle ID | `com.anysphere.sand` |
| 观察版本 | `0.71.2`（`CFBundleShortVersionString`） |
| 数据目录 | `~/Library/Application Support/Grok Bot/` |
| 其它相关 | `~/.grokbot/`（仅 local-exec / settings，无会话）、`~/Library/Preferences/com.anysphere.sand.plist`、`~/Library/HTTPStorages/com.anysphere.sand` |

不相关 / 不读取：`Cookies`、`sand-secrets.json`、`automation-webhook-keys/`、`~/.grokbot/local-exec-daemon-credential.json` 等凭据类文件；`Local Storage/leveldb`（已于 2026-09 迁移到 `sand-client-persistence`，见 `.migrated-from-local-storage`，现基本为空）；无 `IndexedDB`。

### 2.2 位置

```
PERSIST_DIR = ~/Library/Application Support/Grok Bot/sand-client-persistence/
  <base32(sliceKey)>.blob     # 每个 slice 一个文件，明文 JSON（UTF-8）
  .migrated-from-local-storage
```

### 2.3 文件名解码

文件名（去掉 `.blob`）是 **小写、无 padding 的 RFC 4648 base32**：

```python
def decode_blob_name(filename: str) -> str:
    s = filename[:-len(".blob")].upper()
    s += "=" * (-len(s) % 8)
    return base64.b32decode(s).decode("utf-8")
```

### 2.4 Slice 命名

解码后的 key 形如：

```
sand.client.slice.<slice>                                   # 全局 slice
sand.client.slice.account.<urlencoded accountId>.<slice>    # 账号级 slice
```

- `accountId` 经 URL 编码（例：`google-oauth2%7Cuser_XXXX` → `google-oauth2|user_XXXX`），用 `urllib.parse.unquote` 还原。
- 已知 slice（来自文件与 App bundle 中的常量表）：`roster.last-roster`、`roster.agent-avatars`、`selection.last-agent`、`send-journal`、`sidebar.last-sections`、`transcript.replicas.<agentId>`、`ui-agent-refs`、`ui-layout`、`weekly-usage.cache`、`experiments.last-latched-gates`、`connection.last-host-capabilities`、`bot-templates.export-policy`、`client-meta.account-slot`、`first-run.device-onboarded` 等。
- 本 spec 只使用 `transcript.replicas.<agentId>` 与 `roster.last-roster`。

### 2.5 通用 envelope

所有 blob：

```json
{"schemaVersion": <int>, "value": { ... }}
```

### 2.6 `transcript.replicas.<agentId>`（schemaVersion 观察值 = 1）

```json
{
  "schemaVersion": 1,
  "value": {
    "entries": [ <Entry>, ... ],
    "epochHint": null,
    "acceptedSequenceHint": null,
    "persistedAt": 1760000000000,
    "cloudAgentPeerIds": [ ... ]        // 可选，部分 bot 才有
  }
}
```

`agentId` = slice key 最后一段（UUID，36 字符），与 roster `rows[].id` 对应。

### 2.7 Entry 结构

公共字段：`kind`（str）、`id`（str，单 bot 内唯一）、`seq`（int，单 bot 内单调递增）、`timestampMs`（int，epoch ms）、`requestId`（可选）。

| kind | 关键字段 | 说明 |
|---|---|---|
| `message` | `role`(`user`/`assistant`)、`content`(str)、`isStreaming`(bool)、可选 `fromAgent`、`toAgent`、`richText`、`clientNonce`、`batchId` | `role=user` 且无 `fromAgent` = 人类输入；`role=user` 且有 `fromAgent` = 其它 bot 发来；`role=assistant` 且有 `toAgent` = 发给其它 bot |
| `send-message` | `message{type,...}`、可选 `widgetSkipped`、`respondedValue`、`boxInstruction`、`boxReason`、`boxRequestId`、`boxResolution`、`spendRequestId` | bot 对用户的输出。`message.type` 观察值：`text`、`widget`、`attachment`、`cursor-agent`、`auto-review-approval`、`local-tool-permission`、`secret-request` |
| `user-attachment` | `file_name`、`file_path`、`batchId`、`clientNonce`、可选 `byteSize`、`width`、`height` | 用户附件 |
| `notice` | `text` | 简短系统/助手提示 |
| `event` | `event{...}` | 重命名、成员变更等簿记 |

`send-message.message` 子结构：

| type | 字段 |
|---|---|
| `text` | `content`(str) |
| `attachment` | `file_name` / `url` 等 |
| `widget` | `widget{...}` |
| `cursor-agent` | `bcId`、`title` |
| `auto-review-approval` | `approval{...}` |
| `secret-request` | `secretRequest{...}` |
| `local-tool-permission` | 权限卡片 |

### 2.8 渲染 vs 跳过

| 输入 | 处理 |
|---|---|
| `message` role=user 且无 `fromAgent` | **User** 段，`content` |
| `message` role=user 且有 `fromAgent` | Assistant 侧，前缀 `From <fromAgent>:`（或跳过，见配置 `grokbot_include_agent_mail`，默认渲染） |
| `message` role=assistant | Assistant 段，`content`（有 `toAgent` 时前缀 `To <toAgent>:`） |
| `message` `isStreaming == true` | **跳过**（流式未完成的中间态；同 `id` 的最终版会覆盖，见 §5） |
| `send-message` type=`text` | Assistant 段，`message.content` |
| `send-message` type=`attachment` | Assistant 段，`` Attached: `<name>` `` |
| `send-message` type=`cursor-agent` | Assistant 段一行备注：`[Cloud agent: <title>]` |
| `send-message` 其它 type（widget / approval / permission / secret-request） | **跳过** |
| `user-attachment` | User 段，`` Attached: `<file_name>` `` |
| `notice` | Assistant 段，`text` |
| `event` | **跳过** |
| 未知 kind | 跳过并计数（`--verbose` 时打印） |

所有文本经 `render_common.clean_text()`。

### 2.9 `roster.last-roster`（schemaVersion 观察值 = 1）

```json
{"schemaVersion": 1, "value": {"rows": [ {
  "id": "<agentId>", "name": "str", "description": "str", "title": "str",
  "avatarShape": "str", "avatarColor": "str", "avatarVersion": null, "avatarPhoto": null,
  "createdAt": <ms>, "updatedAt": <ms>,
  "path": "/home/box/agent-data/agents/<agentId>/store.db",   // 可能为空串
  "lastEntry": {"kind": "str", "text": "str"},
  "lastMessageId": "str", "newestEntryId": "str",
  "hasUnread": false, "unreadCount": 0,
  "lastViewedAt": <ms>, "lastActivityAt": <ms>, "localActivityAt": <ms>,
  "awaitingUserResponse": null, "notificationsEnabled": true,
  "notifyOnUpdatesEnabled": true, "isHiddenFromSidebar": false,
  "voiceId": null, "voiceSpeed": 1
} ]}}
```

用途：`agentId → name`（作为 project / 子目录名）、`createdAt`（会话起点兜底）、`isHiddenFromSidebar`（元信息）。roster 中不存在的 replica（已删除的 bot）仍导出，名称回退为 `agentId`。

---

## 3. 限制：约 200 条滚动窗口

本地 replica 只是客户端缓存，**每个 bot 仅保留最近约 200 条 entry**。2026-10-15 观察（仅计数）：

| agent（roster name） | entries | seq 范围 | 结论 |
|---|---|---|---|
| 小布 | 200 | 145–348 | 已截断（缺 seq 1–144） |
| Grok Bot | 200 | 68–291 | 已截断（缺 seq 1–67） |
| GTMaster | 188 | 1–188 | 完整 |
| 发X的 | 106 | 1–123 | 完整（seq 有空洞：被过滤 / 隐藏的条目） |
| New Bot (de0d…) | 97 | 1–109 | 完整 |
| New Bot (fc62…) | 46 | 1–54 | 完整 |
| cursorgrokbot | 6 | 1–7 | 完整 |

推论：

- `seq` 不连续是正常的（隐藏条目），**不能**用“seq 连续”判断完整性；只有 **首条 seq > 1** 才判定为“开头被截断”。
- 窗口上限 200 是观察值，未在代码中确认，实现中不得硬编码依赖。
- 截断部分无法从本地恢复；只能靠 §5 的归档在被挤出窗口之前保存下来。

---

## 4. 设计

### 4.1 `grokbot_md.py`

```python
import base64, json, os, sqlite3, urllib.parse

DEFAULT_PERSIST_DIR = os.path.expanduser(
    "~/Library/Application Support/Grok Bot/sand-client-persistence")
REPLICA_MARK = ".transcript.replicas."
ROSTER_SUFFIX = ".roster.last-roster"
KNOWN_REPLICA_SCHEMAS = {1}
KNOWN_ROSTER_SCHEMAS = {1}


def decode_blob_name(filename: str) -> str | None:
    if not filename.endswith(".blob"):
        return None
    s = filename[:-5].upper()
    try:
        return base64.b32decode(s + "=" * (-len(s) % 8)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _read_envelope(path: str) -> tuple[int | None, dict | None]:
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(obj, dict) or not isinstance(obj.get("value"), dict):
        return None, None
    return obj.get("schemaVersion"), obj["value"]


def iter_replicas(root: str = DEFAULT_PERSIST_DIR):
    """Yield dicts: agent_id, account_id, path, entries(sorted by seq), mtime, schema."""
    if not os.path.isdir(root):
        return
    for fn in sorted(os.listdir(root)):
        key = decode_blob_name(fn)
        if not key or REPLICA_MARK not in key or ".account." not in key:
            continue
        acct_part, agent_id = key.split(REPLICA_MARK, 1)
        account_id = urllib.parse.unquote(acct_part.split(".account.", 1)[1])
        path = os.path.join(root, fn)
        schema, value = _read_envelope(path)
        if value is None:
            warn(f"(grokbot) unreadable replica {fn}")
            continue
        if schema not in KNOWN_REPLICA_SCHEMAS:
            warn(f"(grokbot) replica schemaVersion={schema} untested; parsing best-effort")
        entries = [e for e in value.get("entries") or [] if isinstance(e, dict)]
        entries.sort(key=lambda e: (e.get("seq") or 0, e.get("timestampMs") or 0))
        yield {"agent_id": agent_id, "account_id": account_id, "path": path,
               "entries": entries, "mtime": os.path.getmtime(path), "schema": schema}


def load_roster(root: str = DEFAULT_PERSIST_DIR) -> dict[str, dict[str, dict]]:
    """{account_id: {agent_id: row}}"""
    out: dict[str, dict[str, dict]] = {}
    if not os.path.isdir(root):
        return out
    for fn in os.listdir(root):
        key = decode_blob_name(fn)
        if not key or not key.endswith(ROSTER_SUFFIX) or ".account." not in key:
            continue
        acct = urllib.parse.unquote(key.split(".account.", 1)[1][: -len(ROSTER_SUFFIX)])
        _, value = _read_envelope(os.path.join(root, fn))
        rows = (value or {}).get("rows") or []
        out[acct] = {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}
    return out


def parse_entries(entries, include_thinking: bool = False) -> dict:
    """Shared renderer core: entries(list[dict]) -> {sections, started, ended, first_seq, skipped}.
    现有 parse_store() 中的 for 循环原样搬到这里，并补充：
      - message.isStreaming is True -> skip
      - send-message type cursor-agent -> "[Cloud agent: <title>]"
      - message fromAgent / toAgent -> 前缀
      - 未知 kind -> skipped += 1
    """


def parse_store(path: str, include_thinking: bool = False) -> dict:
    """回退源：store.db -> entries -> parse_entries()。"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT entry FROM transcript_entries ORDER BY seq").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    entries = [e for e in (_loads(r[0]) for r in rows) if e]
    return parse_entries(entries, include_thinking)


def grokbot_entries_to_md(entries, meta_extra=None, include_thinking=False,
                          title=None, truncated_before_seq: int | None = None) -> dict:
    """返回与 grokbot_to_md 相同的 dict：markdown/title/first_prompt/started/ended/user_turns。
    truncated_before_seq 不为 None 时，在 meta 中加入
      "- Note: history before seq <N> not available locally"
    """


def grokbot_to_md(path, meta_extra=None, include_thinking=False, title=None) -> dict:
    """保留：store.db 版本，内部改为 parse_store -> grokbot_entries_to_md 的同一渲染路径。"""
```

### 4.2 `export_sessions.py`

```python
def _grokbot_persist_dir(config_value: str | None) -> str:
    return os.path.expanduser(config_value or grokbot_md.DEFAULT_PERSIST_DIR)


def grokbot_source_present(explicit: str | None, persist_dir: str | None = None) -> bool:
    d = _grokbot_persist_dir(persist_dir)
    if os.path.isdir(d) and any(
            (grokbot_md.decode_blob_name(f) or "").find(".transcript.replicas.") >= 0
            for f in os.listdir(d)):
        return True
    # 回退：原 store.db / .tgz 检测逻辑不变
    ...


def grokbot_tasks(thinking, project, data_path, account_override,
                  persist_dir=None, out_root=None):
    persist = _grokbot_persist_dir(persist_dir)
    roster = grokbot_md.load_roster(persist)
    account = account_override or current_cursor_email() or "grokbot"
    seen_agents = set()

    for rep in grokbot_md.iter_replicas(persist):
        agent_id = rep["agent_id"]
        seen_agents.add(agent_id)
        row = roster.get(rep["account_id"], {}).get(agent_id, {})
        bot = (row.get("name") or "").strip() or agent_id
        if project and project.lower() not in f"{bot} {agent_id}".lower():
            continue
        archive = archive_path(out_root, agent_id)          # §5
        merged = merge_into_archive(archive, rep["entries"])  # §5, 返回合并后全量 entries
        key = f"grokbot:{agent_id}"
        version = max(rep["mtime"], _mtime(archive))

        def render(merged=merged, bot=bot, agent_id=agent_id, row=row):
            first_seq = min((e.get("seq") or 0) for e in merged) if merged else None
            extra = {"Account": account, "Source": "Grok Bot (desktop cache)",
                     "Agent": bot, "Session": agent_id}
            res = grokbot_md.grokbot_entries_to_md(
                merged, meta_extra=extra, include_thinking=thinking,
                truncated_before_seq=first_seq if first_seq and first_seq > 1 else None)
            if res["user_turns"] == 0:
                return None
            return {"account": account, "source": "chat", "project": bot,
                    "when": res["started"] or epoch_to_dt(row.get("createdAt")),
                    "title": res["title"], "markdown": res["markdown"],
                    "turns": res["user_turns"], "cwd": None, "git": None}
        yield key, version, "grokbot", render

    # 回退：data_path（grokbot_data）指向 store.db 目录/.tgz 时，沿用现有逻辑，
    # 跳过 seen_agents 中已由 replica 覆盖、且 store.db 无 transcript 行的 agent。
    if data_path:
        yield from _grokbot_store_tasks(thinking, project, data_path, account, skip=seen_agents)
```

### 4.3 Task 元组与 render dict

| 字段 | 值 |
|---|---|
| `key` | `grokbot:<agentId>`（与现有 manifest key 兼容） |
| `version` | `max(replica blob mtime, archive mtime)`；未变化则不重新渲染 |
| `vendor` | `"grokbot"` |
| `render()` | 返回下表 dict，或 `None`（无人类输入 / 被 `--project` 过滤） |

| render dict 键 | 来源 |
|---|---|
| `account` | `grokbot_account` > `current_cursor_email()` > `"grokbot"` |
| `source` | `"chat"`（输出 `grokbot/<account>/chat/<bot>/...`） |
| `project` | roster `name`，缺省 `agentId` |
| `when` | 首条 entry `timestampMs`，缺省 roster `createdAt` |
| `title` | 首个人类 prompt 截 60 字（`_derive_title`） |
| `markdown` | `grokbot_entries_to_md()["markdown"]` |
| `turns` | `user_turns` |
| `cwd` / `git` | `None` |

### 4.4 配置（`config.yml`）

```yaml
# Grok Bot 桌面客户端缓存目录（默认值如下，一般无需设置）
# grokbot_persist_dir: ~/Library/Application Support/Grok Bot/sand-client-persistence
# 回退源：store.db 所在 agent-data 目录或 .tgz（~/git/grokbot 永远忽略）
# grokbot_data: /path/to/agent-data
# 账号归属（默认取 Cursor 登录邮箱）
# grokbot_account: bruce@runningbruce.com
# 是否渲染 bot 之间的消息（fromAgent / toAgent），默认 true
# grokbot_include_agent_mail: true
```

CLI：新增 `--grokbot-persist-dir`，优先级 CLI > config > 默认。`vendor_source_present("grokbot", ...)` 需同时传入 persist dir。

---

## 5. 增量与归档

### 5.1 归档文件

```
<output_dir>/grokbot/.archive/<agentId>.jsonl
```

- 每行一个原始 entry JSON（不做裁剪，保留全部字段，便于以后重新渲染）。
- append-only 语义：已归档的 entry 不因为在 replica 中消失而删除。
- 位于 `output_dir`（本机 / 用户自己的 vault），不写入 App 目录，不写入仓库。
- `.archive` 以 `.` 开头，索引生成与 `prune_manifest` 不应把它当作会话文件。

### 5.2 去重与合并算法

```python
def merge_into_archive(archive: str, window: list[dict]) -> list[dict]:
    old = read_jsonl(archive)                     # 损坏行跳过并计数
    by_id = {e["id"]: e for e in old if e.get("id")}
    changed = False
    for e in window:
        if e.get("isStreaming") is True:          # 不归档流式中间态
            continue
        eid = e.get("id")
        if not eid:
            continue
        prev = by_id.get(eid)
        if prev is None or prev != e:             # 新条目或同 id 被更新（如流式完成、respondedValue）
            by_id[eid] = e
            changed = True
    merged = sorted(by_id.values(),
                    key=lambda e: (e.get("seq") or 0, e.get("timestampMs") or 0))
    if changed:
        atomic_write_jsonl(archive, merged)       # 写临时文件 + os.replace
    return merged
```

- 主键：`id`；排序键：`(seq, timestampMs)`。
- 同 `seq` 不同 `id` 的冲突（理论上不应出现）：两条都保留，并 `warn`。
- 同 `id` 内容变化：以最新 replica 为准（覆盖）。
- 若 `archive` 写失败：本次仍用内存中的 `merged` 渲染，`version` 不更新，下次重试。

### 5.3 缺口标记

- 合并后 `first_seq = min(seq)`；若 `first_seq > 1`，Markdown meta 增加：
  `- Note: history before seq <first_seq> not available locally (desktop cache window)`
- 不对 seq 中间的空洞做标记（隐藏条目导致的空洞是正常的，见 §3）。

### 5.4 运行频率建议

- 窗口约 200 条/bot；活跃 bot 一天可产生 100+ 条（观察：小布 2 天约 200 条）。
- 建议 `runapp.sh` 至少**每天**运行一次；重度使用时每 4–6 小时一次（launchd `StartInterval` 或 cron）。
- 仅在 Grok Bot App 曾打开并同步过该 bot 时，replica 才会更新；长期未打开的 bot 不会产生新数据，但也不会丢失已归档数据。

---

## 6. 回退源：store.db

保留现有 `grokbot_data` 路径（agent-data 目录或 `.tgz`）与 `parse_store()`。

```sql
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB NOT NULL) STRICT;
CREATE TABLE transcript_entries (
  seq INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  entry TEXT NOT NULL          -- 与 §2.7 相同的 Entry JSON
) STRICT;
CREATE TABLE automation_completion_inbox (
  seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, text TEXT NOT NULL,
  attribution TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0
) STRICT;
```

- 位置（box）：`/home/box/agent-data/agents/<agentId>/{store.db, profile.json}`；`profile.json` 键：`name, description, title, avatarShape, avatarColor, namedBy, serverId, harness`。
- 现状（2026-10-15）：所有 bot 的 `transcript_entries` 为空或只有 `event`，**当前不可用**；`kv.metadata` 不是明文 JSON，不解析。
- 若将来 store.db 有数据：同一 `agentId` 的 store.db entries 也通过 `merge_into_archive()` 合并进同一归档（`id` 去重），从而与 replica 互补。
- 获取 store.db 需要用户自行打包（如 `.tgz`）放到本机；本工具不主动远程拉取。

---

## 7. 非目标 / 不做

- **不**调用 Grok Bot / Cursor 的私有服务端 API，**不**读取或复用 Cookies、token、`sand-secrets.json`、`local-exec-daemon-credential.json` 等凭据。
- **不**修改、删除、锁定 App 数据目录中的任何文件（只读打开）。
- **不**上传、同步任何数据到网络。
- **不**渲染 widget、审批卡片、权限请求、secret-request 等 UI 交互条目。
- **不**下载附件本体（只记录文件名）。
- **不**尝试恢复已被挤出窗口、且未归档的历史。

---

## 8. 边界情况与错误处理

| 场景 | 处理 |
|---|---|
| `sand-client-persistence` 不存在（未安装 App / 其它机器） | `grokbot_source_present()` 返回 False → 不 prune 已导出的 grokbot 会话；打印一行提示 |
| 目录存在但无 replica | 同上，0 个会话，不报错 |
| 文件名非 base32 / 解码失败 | 跳过该文件 |
| JSON 损坏 / 半写入（App 正在写） | 跳过该 replica，`warn`；不更新该 agent 的 manifest version（下次重试）；可选重试一次（sleep 0.2s） |
| `schemaVersion` 不在 `KNOWN_*_SCHEMAS` | 尽力解析 + `warn`；若 `value.entries` 不是 list 则跳过该 agent |
| entry 缺 `id` | 不入归档；仍参与本次渲染 |
| entry 缺 `seq` / `timestampMs` | 排序按 0；时间不参与 started/ended |
| `isStreaming == true` | 不渲染、不归档 |
| 未知 `kind` / `message.type` | 跳过并计数 |
| 多账号（多个 `account.<id>` 前缀） | 每个 replica 带 `account_id`；roster 按账号查找；输出账号默认仍用 Cursor 邮箱，若检测到 >1 个 account_id 且未设置 `grokbot_account`，则用 `account_id` 的 slug 区分并 `warn` |
| roster 缺失 / 无对应行（bot 已删除） | 名称回退 `agentId`，仍导出 |
| 同名 bot（如两个 “New Bot”） | 输出目录按 `<name>` 时冲突 → 依赖现有 `reserve()` 消歧；建议 project 名用 `<name>-<agentId[:8]>` 当重名时 |
| 归档文件损坏行 | 跳过该行并 `warn`，其它行照常；重写时丢弃损坏行前先备份为 `.jsonl.bak` |
| 路径位于 `~/git/grokbot` | 维持现有忽略规则 |

---

## 9. 测试计划 / 验收标准

### 9.1 单元测试（新增到 `test_cursor_grokbot.py`，全部用合成数据，不读真实目录）

1. `decode_blob_name`：对 `base64.b32encode(key).lower().rstrip("=") + ".blob"` 往返一致；非法名返回 `None`。
2. `iter_replicas`：临时目录写入 2 个账号、3 个 replica + 1 个非 replica + 1 个损坏 JSON；断言返回 3 条、`account_id` 已 unquote、entries 按 seq 排序、损坏文件被跳过。
3. `load_roster`：按账号返回 `{agentId: row}`。
4. `parse_entries`：覆盖 §2.8 每一行（含 `isStreaming` 跳过、`fromAgent` 前缀、`cursor-agent` 备注、widget 跳过、`event` 跳过、未知 kind 计数）。
5. `parse_store` 与 `parse_entries` 对同一组 entries 输出相同 Markdown（回归现有测试）。
6. `merge_into_archive`：
   - 首次运行写入全部；
   - 第二次窗口前移（丢掉前 50 条、新增 50 条）后归档条数 = 并集；
   - 同 `id` 内容变化被覆盖；
   - `isStreaming` 不入归档；
   - 写入为原子替换。
7. 缺口标记：`first_seq > 1` 时 meta 含 Note；`first_seq == 1` 时不含；中间空洞不标记。
8. `grokbot_source_present`：无目录 False；有 replica True；仅 store.db 回退时仍 True。

### 9.2 集成验收（在本机手动执行）

- `python3 export_sessions.py --vendor grokbot` 在本机输出 **7 个 bot 中有人类输入的全部会话**（当前 0 → ≥ 6），位于 `<output_dir>/grokbot/<account>/chat/<bot>/`。
- 小布、Grok Bot 两个会话的 Markdown meta 中出现缺口 Note（seq 145 / 68）。
- 生成 `<output_dir>/grokbot/.archive/<agentId>.jsonl` 7 个文件。
- 立即再次运行：0 个会话被重新渲染（manifest version 未变）。
- 运行过程中 `~/Library/Application Support/Grok Bot/` 下所有文件 mtime 不变（只读验证）。
- 运行过程中无任何网络请求（可用 `nettop` / Little Snitch 观察）。
- `./runapp.sh` 全流程通过，其它 vendor 结果不变。

---

## 10. 未决问题

1. **窗口上限**：200 是观察值；是否与 App 版本、bot 活跃度相关？升级 App 后需复查。
2. **App 何时写 replica**：是否只在打开对应 bot 时更新？后台 bot 是否同步？影响 §5.4 运行频率建议。
3. **schemaVersion 演进**：replica 目前为 1；升级后字段可能变化，需要版本化解析器。
4. **多账号归属**：`account_id`（如 `google-oauth2|user_…`）与 Cursor 邮箱的映射是否稳定？是否应改为以 `account_id` 为主？
5. **bot 间消息**（`fromAgent` / `toAgent`）是否应单独成段或默认隐藏？
6. **`cloudAgentPeerIds` / `cursor-agent` 条目**：是否需要关联 box 上 `/workspace/cloud-agent-transcripts/bc-*.jsonl` 的云端子 agent 记录（在 box 上，不在 Mac）？本 spec 暂不处理。
7. **完整历史**：是否会有官方导出功能（类似 ChatGPT Data Export）？若有，应作为第三个数据源接入，并与归档按 `id` 合并。
8. **附件**：`user-attachment.file_path` 指向 box 路径，Mac 上不可得；是否需要关联 `attachment-image-cache/`？
