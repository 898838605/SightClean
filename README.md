# SightClean

面向 Windows 10 / 11 的免费开源清理工具。**带用途标注的资源管理器**——每一项都告诉你它是什么、删了会怎样，删不删你说了算。

不扫全盘、不替你做决定、不偷偷联网。

- **免安装**：单个 exe，22.5 MB，不装运行库，拷到哪都能跑
- **开源**：Apache-2.0，代码全部公开，可自行审阅、修改、分发
- **通用**：不针对某一台机器，盘符、路径、已装软件全部运行时读取

---

## 目录

- [一、它是什么](#一它是什么)
- [二、快速开始](#二快速开始)
- [三、四大模块](#三四大模块)
  - [3.1 文件清理](#31-文件清理)
  - [3.2 软件卸载](#32-软件卸载)
  - [3.3 注册表清理](#33-注册表清理)
  - [3.4 进程管理](#34-进程管理)
- [四、配置文件详解](#四配置文件详解)
- [五、文件放在哪](#五文件放在哪)
- [六、常见问题](#六常见问题)
- [七、开发与打包](#七开发与打包)

---

## 一、它是什么

### 和常见清理软件的区别

| | 常见清理软件 | SightClean |
|---|---|---|
| 扫描方式 | 一打开就全盘扫 | **你点哪里它才看哪里** |
| 判定依据 | 黑盒打分、玄学“垃圾” | 每条都写明**它是什么、删了会怎样** |
| 决定权 | 它替你勾好，你点确定 | **默认一个都不勾**，你看着勾 |
| 联网 | 常年后台联网 | **只有你点 AI 按钮时才联网** |
| 失败处理 | “清理失败”四个字 | 说清是**被哪个程序占着**、还是**没权限** |

### 硬性约束

- **单 exe**：22.5 MB，免安装、免运行库，双击即用
- **启动 2 秒内**：先画窗口，再异步加载盘符和目录
- **中文界面、零术语**：不说“句柄”“孤儿键”“重定向”，只说人话
- **长路径**：超过 260 字符的路径用 `\\?\` 前缀处理，中文路径同样没问题
- **配置坏了也不崩**：任何一个字段写错都退回默认值，并在启动时告诉你

---

## 二、快速开始

1. 双击 `SightClean.exe`（会弹 UAC，点“是”）
2. 左侧四个模块，点哪个进哪个
3. 看到想删的，勾上 → 点“删除选中”

**默认要管理员权限**。原因：`Program Files`、`HKLM` 注册表、系统进程这些位置普通权限动不了。与其让你到处撞“需要管理员权限”，不如一上来就拿够。

---

## 三、四大模块

### 3.1 文件清理

#### 实现思路

这是一个**不会自己跑的资源管理器**。

核心设计：**你导航到哪个目录，它才读哪个目录**。没有“扫描全盘”这个动作。

**分层判定（`core/knowledge.py`）**

知识库有 87 条规则，分三层，从具体到宽泛：

| 层 | 判断依据 | 例子 | 条数 |
|---|---|---|---|
| 第 1 层 | **路径特征**（最准） | `\SoftwareDistribution\Download\` → Windows 更新安装包 | 38 |
| 第 2 层 | **文件名/扩展名** | `.vmdk` → 虚拟机磁盘文件 | 38 |
| 第 3 层 | **兜底规则** | 未知项统一给“未收录，建议确认后再删” | 11 |

匹配方式有 7 种：`path_contains`、`path_regex`、`ext`、`name_contains`、`name_exact`、`key_prefix`、`key_contains`。

**风险分三级**

- 🟢 **可以删**：确定是缓存/临时文件，删了会自动重建
- 🟡 **看情况**：可能是你的资料，也可能是垃圾
- 🔴 **别删**：系统文件，删了会出问题

**风险等级不参与拦截。** 红、黄、灰只影响颜色和提示，所有项删除前都走同一张确认清单，每一项都能单独取消。若选择“彻底删除”，还必须额外勾选一次不可撤销确认。程序把后果写清楚，但不替用户做决定。

每条规则除了 `effect`（这是什么）还有 `impact`（删了会怎样），两个都显示在“作用”列。

**大小计算（`core/size_calc.py`）**

- 一边遍历一边累加，不是先收集再算
- 并发数可调（默认 2），因为机械硬盘开太多线程反而慢
- 结果缓存在 `cache.json`，默认 24 小时有效
- 遇到符号链接不跟进（防止循环）

**删除（`core/delete.py`）**

两种模式：

- **回收站**（默认）：走 `send2trash`，能还原
- **彻底删除**：直接删，不能还原

失败时**不笼统报错**，而是查真实原因：

```
这个目录里的程序正在运行：chrome.exe。先关掉它们再试
这个文件被别的程序打开了，先关掉它再试
需要管理员权限（这个位置普通权限动不了）
```

怎么知道是谁占着？用 `psutil` 枚举所有进程，看哪个的 exe 路径落在目标目录下。

#### 功能

| 功能 | 说明 |
|---|---|
| 目录浏览 | 双击进目录、“上一级”返回、面包屑跳转 |
| 作用标注 | 每行显示这是什么 + 删了会怎样 |
| 大小计算 | 点“算一下多大”，可中途停止 |
| 勾选删除 | 单个勾 or 表头全选，进回收站或彻底删 |
| 搜索过滤 | 按名称/扩展名过滤，支持多关键词 |
| 排序 | 点表头升序/降序/取消三态 |
| 导出 CSV | 把当前列表导成表格 |
| **一键清理 C 盘垃圾** | 按规则批量清 5 类垃圾（见下） |

#### 一键清理 C 盘垃圾

规则写在 `垃圾清理规则.json` 里，默认五类：

| 类别 | 扫哪里 | 默认勾选 |
|---|---|---|
| 临时文件 | `%TEMP%`、`%WINDIR%\Temp` | ✅ |
| 系统日志 | `%WINDIR%\Logs`、`System32\LogFiles`、WER 报告 | ❌ 标“看情况” |
| 缩略图缓存 | `thumbcache_*.db`、`iconcache_*.db` | ✅ |
| 浏览器缓存 | Chrome / Edge / Firefox / Brave 的 Cache | ✅ |
| 包管理器缓存 | pip、npm、Yarn、pnpm | ✅ |

界面**按类别勾选，不逐条列**——一台机器动辄十几万项，列出来既没用又卡。

每类下面列出**实际路径**，**双击能跳到资源管理器**去实地看看。

删除走的是**彻底删除**，不进回收站。原因：回收站也在 C 盘，把 10 GB 缓存“移”进去等于没清。

#### 怎么用

```
1. 点左侧「文件清理」
2. 双击进目录，看「作用」列写的什么
3. 想删的勾上 → 点「🗑 删除选中」
4. 弹窗告诉你删了几个、几个没删掉、为什么
```

---

### 3.2 软件卸载

#### 实现思路

**三个数据源合并（`core/providers/`）**

单一数据源都有盲区，所以合并三份：

| 来源 | 读什么 | 补什么盲区 |
|---|---|---|
| **注册表** | `HKLM/HKCU\...\Uninstall\*`，4 个 WOW64 视图 | 大部分传统软件 |
| **winget** | `winget list` 输出 | 商店应用、部分新式安装器 |
| **孤儿检测** | 扫 `Program Files` 有目录但注册表没登记的 | 手动解压的、卸载残留的 |

合并策略：**注册表优先**，同名的用注册表那份，winget 和孤儿只做补充。

winget 输出是**等宽表格**，中英文混排时列会对不齐，所以用 `unicodedata.east_asian_width` 按显示宽度切分，不能按空格切。

**卸载器类型识别（`core/installers.py`）**

不同安装器静默卸载参数不同，认错了就卸载失败：

| 类型 | 识别特征 | 静默参数 |
|---|---|---|
| MSI | `MsiExec.exe /I{GUID}` | `/X{GUID} /qn` |
| NSIS | `uninst.exe` | `/S` |
| Inno Setup | `unins000.exe` | `/VERYSILENT` |
| Steam | `steam://uninstall/` | — |
| Squirrel | `Update.exe --uninstall` | — |

MSI 那个要特别注意：注册表里常写成 `/I{GUID}`（安装），必须换成 `/X{GUID}`（卸载），否则会**重新安装**。实际扫描中这类写错的项很常见。

**残留扫描（`core/leftover.py`）**

卸载后可能有五类残留：

1. **安装目录**（`InstallLocation` 指向的）
2. **注册表键**（`HKLM\SOFTWARE\<厂商>\<软件>`）
3. **快捷方式**（开始菜单、桌面）
4. **Prefetch**（`.pf` 预读文件）
5. **WER / AppCompatFlags**（崩溃报告、兼容性标记）

怎么知道该删哪些？**挖 exe 名字**：

- 从 `DisplayIcon` 取值
- 从 `UninstallString` 取
- 扫安装目录下的 `.exe`
- **UserAssist**（`HKCU\...\UserAssist\*`）里记录着用户运行过的程序，值是 ROT13 编码的，解码后能挖出历史运行过的 exe

拿到 exe 名后去匹配 `Prefetch\<名字>-*.pf`、`WER\ReportArchive\*<名字>*`。

**AI 识别残留（`core/ai_leftover.py`）**

手动按钮才触发，能识别“规则认不出来”的残留：

- 候选：不属于任何已装软件的目录/注册表键
- 每批 25 个交给 AI，最多 150 个
- **带上下文的**：告诉 AI 完整已装软件列表（167 个名字），不然它会把正常软件误判成残留
- 卸载单个软件时，AI **只看跟这个软件有关**的痕迹

#### 功能

| 功能 | 说明 |
|---|---|
| 软件列表 | 名称、版本、发布者、安装日期、占用大小、是否清理 |
| 卸载 | 调系统卸载器；可复制识别出的静默命令 |
| 强制卸载 | 卸载器坏了时的兜底，会先备份 |
| 残留扫描 | 卸载后自动问“要不要扫残留” |
| 扫描位置 | 自己勾要扫哪些目录（17 个范围，含全盘 Program Files） |
| **AI 扫描卸载残余文件夹** | 全机器扫一遍，找不属于已装软件的目录 |

#### 怎么用

```
单个软件：
1. 点「软件卸载」
2. 勾选软件 → 点「卸载选中」
3. 卸载器跑完，弹窗问「要不要扫残留」
4. 扫出来的默认不勾，你看理由再决定

批量清残留：
1. 点「🧹 AI 扫描卸载残余文件夹」
2. 它把整机不属于任何已装软件的目录列出来
3. 交给 AI 判断，结果默认不勾
```

---

### 3.3 注册表清理

#### 实现思路

**三层结构，逐层深入**

| 模式 | 看到什么 | 用途 |
|---|---|---|
| 浏览 | 键树（HKLM/HKCU/HKCR/HKU/HKCC），点进去看子键 | 找东西 |
| 本页未知项 | 当前层里规则认不出的 | 交给 AI |
| 失效项扫描 | **指向的目标已经不存在**的项 | 一键清 |

**作用标注**

知识库里有 `key_prefix` 和 `key_contains` 两类规则，比如：

- `hklm\system` → 系统底层配置，别碰
- `controlset` → 开机时的服务启动配置

**删除：键级 vs 值级**

这是两个不同的操作，必须分开：

- **删键**（`delete_key_tree`）：整个子树递归删
- **删值**（`core/regvalue.py`）：只删某个键下的一个值

为什么分开？`AppCompatFlags` 这类残留是**值**不是键，用删键的方法会报“键不存在”，然后整批中止。

**删除失败要说清原因（`_diagnose`）**

Windows 删注册表失败时，`winerror 5`（拒绝访问）有两种完全不同的含义：

- 下面还有子键
- 真的没权限

同一份代码。所以失败后**回头查一遍这个键现在的状态**：

```python
def _diagnose(hive, sub, exc):
    # 先看它现在什么样：还在吗？能读吗？几个子键？
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as key:
            sub_count, _, _ = winreg.QueryInfoKey(key)
            readable = True
    except FileNotFoundError:
        return "这个键已经不在了"
    except PermissionError:
        readable = False
    ...
```

五种情况的文案：

```
这个键已经不在了
根键不能删
「SECURITY」读不到也删不掉：对你的账户设了拒绝访问。这类键通常归 SYSTEM 或受保护组件所有
「X」下面还有 2 个子键没删掉（其中某个被保护着）
「X」能读却删不掉：权限里没给「删除」，得先改所有者
```

**失效项扫描（`core/registry_scan.py`）**

核心原则：**不做主观猜测，只做客观核对**。

一个注册表项如果**指向的东西已经不存在**，那它一定无效，删了不会影响任何东西。

扫四类：

| 类别 | 判断依据 | 你这台扫到 |
|---|---|---|
| 程序路径登记（App Paths） | 登记的 exe 找不到了 | 2 个 |
| 卸载登记 | 安装目录、图标、卸载程序**全都不在** | 0 个 |
| 共享 DLL 引用（SharedDLLs） | 引用的 DLL 文件不在了 | 94 个 |
| 开机启动项（Run/RunOnce） | 指向的程序不在了 | 2 个 |

**明确不做的**：看起来像垃圾的键、很久没改过的键、**空键**（有些程序先建空键再填内容）。

**WOW64 陷阱**

`HKLM\...\SharedDLLs` 在 32 位和 64 位视图下是**两个不同的键**：

```
64 位视图        97 个值
32 位视图       113 个值
只在 32 位里的   25 个
```

用 `KEY_WOW64_32KEY` 打开时，实际读的是 `SOFTWARE\WOW6432Node\...`，但**你手里的路径字符串还是不带 WOW6432Node 的**。删除时按那个字符串打开，拿到的是 64 位那个键，里面没这个值 → 报“已经不在了”。

所以扫描时**不用 WOW64 标志**，直接写两条真实路径。

**删除前必须备份**

每条删除都先用 `reg export` 导出成 `.reg`，放在 `backups/` 里。备份的是**整个子树**，不是单个键，所以能完整还原。

#### 功能

| 功能 | 说明 |
|---|---|
| 键树浏览 | 五个根键、逐层展开、面包屑 |
| 作用标注 | 每个键告诉你是什么 |
| 清理选中 | 带备份，支持整个子树 |
| **清理注册表** | 按规则找失效项（4 类） |
| **让 AI 找注册表残留** | AI 判断哪些是已卸载软件的残留 |
| 还原备份 | 从 `.reg` 导回去 |

#### 怎么用

```
按规则清（推荐，安全）：
1. 点「注册表清理」
2. 点「🧹 清理注册表」
3. 弹出找到多少失效项，确认
4. 清单默认不勾，看理由再决定
5. 清理前自动备份成 .reg

手动找：
1. 点进 HKLM/HKCU 逐层看
2. 「作用」列写着这是什么
3. 勾选 → 「清理选中」
```

---

### 3.4 进程管理

#### 实现思路

**只做三件事**

把现在跑着什么列出来、标出哪些是系统命脉、把用户勾选的结束掉。

不做实时刷新、不做后台监控——那是任务管理器的事。

**系统关键进程标红**

`CRITICAL_NAMES` 里有 20 多个名字（`smss.exe`、`csrss.exe`、`lsass.exe`、`svchost.exe`…），这些结束基本就是蓝屏或立刻重启。**只标红，不阻止你勾选**——决定权在你。

内存里加了 WSL 相关的 `MemCompression`、`vmmem`——它们不是普通程序，结束会连带影响。

**结束进程：先礼后兵**

```python
proc.terminate()      # 先好好说
time.sleep(1)
if proc.is_running():
    proc.kill()       # 不听再动手
```

**一键清理空闲进程**

判据三条：

| 判据 | 默认值 | 为什么 |
|---|---|---|
| **开了多久** | 30 分钟以上 | 刚打开的不算 |
| **在不在前台** | 不在前台 | 你正在用的那个不动 |
| **CPU** | 低于 1% | 正在算东西的不管 |

再加几层排除：

1. **系统进程**（名字在黑名单里）
2. **服务会话**（会话 ID = 0 或 -1）
3. **桌面 shell**（`explorer.exe`、`StartMenuExperienceHost.exe`、`SearchHost.exe`…，动了就没桌面了）
4. **非当前用户**的进程

**为什么用会话 ID 而不是用户名**

Windows 用会话隔离服务和用户程序：

```
会话 0    = 服务会话（WUDFHost、spoolsv、各种厂商服务都在这）
会话 1+   = 用户登录会话
会话 -1   = 查不到（受保护进程）
```

比看用户名靠谱——普通权限也能查，而且系统服务一律在 0。

**为什么还要按父进程过滤**

微信会拉起 9 个 `WeChatAppEx.exe`，WorkBuddy 会拉起 `node.exe`。这些**属于正在运行的软件**，不该被当成“忘了关的”。

判断依据是父进程还在不在：

```python
if proc.ppid and proc.ppid in alive and proc.ppid not in adopted_pids:
    continue   # 父进程还活着，它是那个程序的一部分
```

`adopted_pids` 是 `explorer.exe`、`cmd.exe`、`svchost.exe` 这类——被它们拉起的算“独立启动”。

**AI 识别进程作用**

进程的 `identity` 是 PID，但发给 AI 的是 **exe 路径**（PID 对 AI 没意义）。回填时按 exe 匹配，同一程序开多个实例会一起更新。

结果**记在内存里**，刷新后不丢——结束一个进程会重建整个列表，不记的话刚识别出来的“作用”就全空回去了。

#### 功能

| 功能 | 说明 |
|---|---|
| 进程列表 | 名称、PID、用户、内存、CPU、路径、作用、风险 |
| 系统进程标红 | 结束会蓝屏的标出来 |
| 结束进程 | 单个或批量，先 terminate 再 kill |
| **一键清理进程** | 按“开得久 + 不在前台 + CPU 低”找空闲进程 |
| 识别本页未知项 | AI 问一遍知识库不认识的进程 |
| 右键菜单 | 打开文件位置、复制 PID、取消勾选 |

#### 怎么用

```
清理忘了关的软件：
1. 点「进程管理」
2. 点「🧹 一键清理进程」
3. 列表按「开了多久」排序，每组显示时长和 CPU
4. 默认不勾，你看着勾 → 点「结束选中的进程」

单杀一个：
1. 找到它，勾上
2. 点「⛔ 结束进程」
```

---

## 四、配置文件详解

程序旁边（或用户目录，见下节）会有这些文件：

```
SightClean.exe
config.json          设置
知识库.json           你加的规则
垃圾清理规则.json      一键清理的定义
cache.json           大小缓存
feedback.json        反馈记录
logs/                运行日志
backups/             注册表备份
```

### 4.1 config.json

程序设置。**所有字段都可以不写**，缺了就用默认值；写错了会退回默认并告诉你。

`schema` 是文件格式版本号，程序用来判断要不要做兼容处理。**手动改配置时保持它不动**。

```json
{
  "schema": 1,
  "auto_size": true,
  "auto_size_concurrency": 2,
  "confirm_recycle": false,
  "show_hidden": false,
  "size_cache_ttl_hours": 24,
  "log_keep_count": 30,
  "recycle_warn_over_gb": 20,
  "window_width": 1200,
  "window_height": 760,
  "last_dir": "",
  "last_module": 0,
  "user_rules": [],
  "scan_scopes": [],
  "theme": "system",
  "font_scale": "medium",
  "ai": {
    "enabled": false,
    "provider": "custom",
    "base_url": "",
    "model": "",
    "api_key": "",
    "mask_username": true,
    "timeout_seconds": 15,
    "max_batch": 20,
    "temperature": 0.1
  }
}
```

#### 界面相关

| 字段 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `window_width` | 1200 | 900–10000 | 窗口宽度 |
| `window_height` | 760 | 560–10000 | 窗口高度 |
| `theme` | `system` | system/light/dark | 跟着系统 or 强制亮/暗 |
| `font_scale` | `medium` | small/medium/large | 字号缩放 |
| `last_module` | 0 | 0–3 | 上次停在哪个模块（0文件/1软件/2注册表/3进程） |
| `last_dir` | `""` | — | 上次浏览的目录，下次打开回到这儿 |

#### 行为相关

| 字段 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `auto_size` | `true` | — | 进目录时自动算大小 |
| `auto_size_concurrency` | 2 | 1–8 | 同时算几个目录。机械硬盘别开太大 |
| `show_hidden` | `false` | — | 显示隐藏文件和系统文件 |
| `confirm_recycle` | `false` | — | 主确认清单之后，是否再问一次才移入回收站 |
| `size_cache_ttl_hours` | 24 | 0–720 | 大小缓存多久过期。0 = 每次都重算 |
| `log_keep_count` | 30 | 1–500 | 保留多少个日志文件 |
| `recycle_warn_over_gb` | 20 | 0–100000 | 进回收站超过多少 GB 时提醒 |

#### 自定义规则

`user_rules` 是你自己加的判定规则，格式和知识库一样：

```json
"user_rules": [
  {
    "id": "my-cache",
    "pattern": "\\myapp\\cache\\",
    "match": "path_contains",
    "effect": "我自己的程序缓存",
    "impact": "删了会重新生成，第一次打开慢一点",
    "risk": "safe"
  }
]
```

#### 扫描范围

`scan_scopes` 是“扫描位置”里勾了哪些。空数组 = 用默认那套（安全的那些）。

可用的 key：

```
appdata          %APPDATA%（漫游数据）
localappdata     %LOCALAPPDATA%（本地数据）
locallow         %LOCALAPPDATA%\Low
packages         %LOCALAPPDATA%\Packages（商店应用数据）
local_programs   %LOCALAPPDATA%\Programs（装在用户目录的程序）
programdata      %PROGRAMDATA%
program_files    所有盘的 Program Files
windows_temp     %WINDIR%\Temp
windows_installer %WINDIR%\Installer（MSI 缓存）
system_profile   系统账户的 AppData
drivers          %WINDIR%\System32\drivers
documents        文档
downloads        下载文件夹
saved_games      保存的游戏
userprofile      整个用户目录
public           %PUBLIC%（公用目录）
package_cache    %PROGRAMDATA%\Package Cache
```

共 17 个，具体哪些可用取决于你这台机器上有没有对应目录（没有的不会列出来）。

#### 主题和字号

`theme`：`system`（跟随系统）、`light`（强制亮色）、`dark`（强制暗色）

`font_scale`：`small`（小）、`medium`（中）、`large`（大）

### 4.2 AI 设置

只有你点 AI 按钮时才会联网。

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关。关着时 AI 按钮点了会引导你去配 |
| `provider` | `custom` | `deepseek` / `kimi` / `openai` / `custom` |
| `base_url` | `""` | API 地址，如 `https://api.deepseek.com/v1` |
| `model` | `""` | 模型名，如 `deepseek-chat` |
| `api_key` | `""` | 你的 key |
| `mask_username` | `true` | **发出去的路径里把用户名换成 `%USER%`** |
| `timeout_seconds` | 15 | 3–120，单次请求超时 |
| `max_batch` | 20 | 1–200，AI 残留判断每批提交多少个候选 |
| `temperature` | 0.1 | 0–2，**越低越死板** |

#### temperature 是什么

控制回答的随机性：

- **0** = 完全确定，同样输入永远给同样答案
- **0.1** = 几乎确定（**推荐**）
- **1.0** = 有明显变化
- **2.0** = 很发散，可能胡说

判断类任务（这是不是残留？这进程是干嘛的？）**建议 0–0.3**。调高了会出现同一个文件两次判断结果不一样的情况。

#### provider 预设

选 `deepseek` / `kimi` / `openai` 时会自动填好 `base_url`。选 `custom` 就全自己填。

只要你的服务兼容 OpenAI 的 `/v1/chat/completions` 接口就能用。

### 4.3 知识库.json

判定规则。内置 87 条，你加的规则会追加在里面。

```json
{
  "schema": 3,
  "version": "2026.11.4",
  "rules": [
    {
      "id": "win-update-download",
      "layer": 1,
      "pattern": "\\softwaredistribution\\download\\",
      "match": "path_contains",
      "effect": "Windows 更新下载到本地的安装包，装完就没用了。",
      "risk": "safe",
      "source": "builtin",
      "impact": "删了不影响系统，下次更新会重新下载"
    }
  ]
}
```

#### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 唯一标识，重复了后面的覆盖前面的 |
| `layer` | ✅ | 1 = 路径规则（最准）、2 = 名字规则、3 = 兜底 |
| `pattern` | ✅ | 匹配用的模式 |
| `match` | ✅ | 匹配方式，见下表 |
| `effect` | ✅ | **这是什么**（显示在“作用”列） |
| `impact` | ✅ | **删了会怎样**（鼠标悬停看详情） |
| `risk` | ✅ | `safe` / `caution` / `forbidden` |
| `source` | — | `builtin` 或 `user` |
| `note` | — | 备注，给自己看的 |

#### 七种匹配方式

| `match` | 拿什么去匹配 | 例子 |
|---|---|---|
| `path_contains` | 完整路径包含 | `\\softwaredistribution\\download\\` |
| `path_regex` | 路径正则 | `^[a-z]:\\boot\\` |
| `ext` | 扩展名 | `vmdk` |
| `name_contains` | 文件名包含 | `visual c++` |
| `name_exact` | 文件名完全相同 | `system` |
| `key_prefix` | 注册表键前缀 | `hklm\\system` |
| `key_contains` | 注册表键包含 | `controlset` |

**写规则的建议**

1. **路径规则用反斜杠要转义**（JSON 里 `\\` 表示一个 `\`）
2. **统一小写**——匹配前会全部转小写
3. **宁可谨慎**——不确定就写 `caution`，别写 `safe`
4. **`impact` 一定要写**——这是这个软件的核心价值

#### 生效优先级

三层规则同时匹配时，**层数小的赢**。同层内，`user` 规则赢 `builtin`。

### 4.4 垃圾清理规则.json

“一键清理 C 盘垃圾”的定义。用户可以改。

```json
{
  "schema": 1,
  "version": "2026.11.15",
  "categories": [
    {
      "id": "temp",
      "label": "临时文件",
      "purpose": "系统或软件运行时产生的临时数据。",
      "detail": "这些文件本来就是用完就该扔的，删了软件会自动重建。",
      "risk": "safe",
      "mode": "contents",
      "paths": ["%TEMP%", "%WINDIR%\\Temp"]
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `id` | 唯一标识 |
| `label` | 显示的名字 |
| `purpose` | 这是什么（对话框里显示） |
| `detail` | 删了会怎样（**风险为 caution 时标黄**） |
| `risk` | `safe` 默认勾选，`caution` 默认不勾 |
| `mode` | `contents` 删目录里的东西、`pattern` 按通配删文件、`whole` 整个删 |
| `paths` | 支持环境变量（`%TEMP%`）和通配符（`*`） |

**`mode` 三种的区别**

- `contents`：目录保留，只删里面的东西（临时文件用这个）
- `pattern`：按通配符匹配到文件删掉（`thumbcache_*.db` 用这个）
- `whole`：整个目录连同本身一起删

**想加一类**

```json
{
  "id": "my-junk",
  "label": "我的程序缓存",
  "purpose": "某程序下载的临时文件。",
  "detail": "删了下次要重新下载。",
  "risk": "safe",
  "mode": "contents",
  "paths": ["%LOCALAPPDATA%\\MyApp\\Cache"]
}
```

**想禁用一类**：直接把那一整段删掉，或者把 `risk` 改成 `caution`（默认就不勾了）。

### 4.5 cache.json

大小计算结果缓存。删了没关系，下次会重算。

```json
{
  "schema": 1,
  "entries": {
    "D:\\some\\directory": {
      "size": 1073741824,
      "files": 1234,
      "at": 1763170000
    }
  }
}
```

超过 `size_cache_ttl_hours` 就失效。**手动删掉这个文件**等于强制全部重算。

### 4.6 feedback.json

“其他问题反馈”记录，纯本地，不上传。

```json
{
  "schema": 1,
  "items": [
    {
      "at": 1763170000,
      "kind": "wrong_effect",
      "path": "D:\\x\\y",
      "note": "这个文件不是垃圾"
    }
  ]
}
```

### 4.7 logs/ 和 backups/

**logs/**：每次操作一个文件，只保留最近 `log_keep_count` 个。

```
删除-20261115-173045.txt
清理-20261115-174022.txt
结束进程-20261115-175500.txt
启动耗时.log
错误-20261115-180000.log
```

**backups/**：注册表清理前的备份，`.reg` 格式，双击能直接导回去。

```
regclean_20261115_173045.reg
```

---

## 五、文件放在哪

**优先放在 exe 旁边**（便携模式）。如果那个位置写不了（比如放在 `C:\Program Files` 但没权限），会改放到：

```
%LOCALAPPDATA%\SightClean\
```

启动时会提示：“程序所在的文件夹不能写东西，设置改放到用户目录了”。

老版本曾用 `%LOCALAPPDATA%\DiskCleanupHelper\`；升级后第一次启动会自动改名迁移，迁不动就继续用老目录，配置不会丢。

---

## 六、常见问题

**Q：为什么默认要管理员权限？**

`Program Files`、`HKLM` 注册表、系统进程这些位置普通权限动不了。与其让你到处撞“需要管理员权限”，不如一上来就拿够。

**Q：AI 会看到我的文件名吗？**

会，但路径里的用户名会替换成 `%USER%`（`mask_username` 默认开）。只有你点 AI 按钮时才发，不点不联网。

**Q：删除失败提示“被程序占用”怎么办？**

按提示关掉对应的程序再删。程序是通过 `psutil` 查哪个进程的 exe 在目标目录下得出的结论。

**Q：注册表删了能还原吗？**

能。每次清理前都会 `reg export` 一份到 `backups/`，双击那个 `.reg` 文件就能导回去。

**Q：一键清理的缓存删了会不会有影响？**

临时文件、缩略图、浏览器缓存、包缓存删了都会自动重建。系统日志标了“看情况”默认不勾，因为它可能是你正在排查问题的线索。

**Q：为什么扫出来的进程一个都没有？**

说明你的机器很干净——所有进程要么是系统进程，要么属于某个正在运行的软件。判断依据是“会话 ID + 父进程 + 路径”，三层过滤。

**Q：配置文件写坏了会怎样？**

不会崩。每个字段单独校验，写错的那项退回默认值，启动时弹窗告诉你哪项有问题。

---

## 七、开发与打包

### 环境

```
Python 3.10
PySide6 6.12
send2trash 2.2
psutil 7.2
PyInstaller 6.22
```

### 目录结构

```
main.py                 入口，先画窗口再加载
app_paths.py            路径解析、长路径前缀
core/
  config.py             设置读写、值校验
  knowledge.py          作用判定（三层规则）
  fs_browser.py         目录浏览
  size_calc.py          大小计算
  delete.py             删除（回收站/彻底）
  uninstall.py          软件枚举、卸载
  installers.py         安装器类型识别
  force.py              强制卸载
  leftover.py           残留扫描与清理
  ai_leftover.py        AI 判断残留
  ai_client.py          AI 通信（stdlib urllib）
  registry.py           注册表键级操作
  regvalue.py           注册表值级操作
  registry_scan.py      失效项扫描
  processes.py          进程枚举、结束、空闲判定
  junk.py               C 盘垃圾清理
  scan_scopes.py        扫描范围定义
  risk.py               风险等级
  snapshot.py           操作快照
  restore.py            还原
  providers/            三个数据源
ui/
  main_window.py        主窗口、工具栏、模块切换
  *_view.py             四个模块的表格
  dialogs.py            各种对话框
  junk_dialog.py        垃圾清理对话框
  process_clean_dialog.py  空闲进程对话框
  leftover_dialog.py    残留清单
  scan_scope_dialog.py  扫描范围选择
  theme.py              明暗主题
  widgets/              过滤栏、CSV 导出等
resources/
  default_config.json   默认设置
  default_knowledge.json 内置规则 87 条
  default_junk.json     垃圾清理规则
  app.ico               图标
SightClean.spec         PyInstaller 打包描述
build.py                打包脚本
```

### 测试

测试套件在 `tests/` 目录里，全部用标准库 `unittest`，不需要额外装测试框架：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

单个文件也能跑：

```bash
python -m unittest tests.test_logic -v
```

测试覆盖纯逻辑（知识库、风险判定、配置兜底）、注册表扫描、删除/回收站回退、AI 请求解析、界面流程等。其中涉及真实磁盘和注册表的部分会在没有对应环境时自动跳过。

验收数据见 [docs/验收记录.md](docs/验收记录.md)。

### 打包

```bash
python build.py
```

或者双击 `build.bat`。

产物：`dist\SightClean.exe`（约 22.5 MB）+ `dist\使用说明.txt`

**打包时要注意的两点**

1. **`SightClean.spec` 里剔掉了一批 Qt 组件**省体积。但 `imageformats\qico.dll` **千万别剔**——那是 Qt 读 `.ico` 的必需品，剔了图标会静默失效（`QIcon` 加载失败不报错，只回退成默认图标）。
2. **datas 用相对路径**时是相对当前工作目录，`build.py` 里用 `cwd=ROOT` 钉死了。

### 加新功能的位置

| 想加什么 | 改哪 |
|---|---|
| 新的文件判定规则 | `resources/default_knowledge.json` |
| 新的垃圾清理类别 | `resources/default_junk.json` |
| 新的注册表失效项类型 | `core/registry_scan.py` 的 `SCANNERS` |
| 新的软件数据源 | `core/providers/` 加一个 source |
| 新的设置项 | `core/config.py` 的 `Settings` + `ui/dialogs.py` 的 `SettingsDialog` |

---

## 许可

**Apache License 2.0** —— 可商用、可修改、可再分发、可私有化，保留版权声明和许可声明即可。

完整条款见 [LICENSE](LICENSE)。
