# UI 与 Figma 同步说明

界面采用 MANAS 的连续工作表面与任务层级，视觉遵循苍渊·白垣 v1.1。默认白垣，苍渊通过同一 Semantic 变量集合切换；两种主题共用结构和交互。

[设计导览](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=127-2) · [全局演示](https://www.figma.com/proto/qBnI3bpm5ULOfoy2ZZrJIC?page-id=88%3A11765&node-id=110-2&starting-point-node-id=110%3A2&scaling=scale-down) · [引擎管理演示](https://www.figma.com/proto/qBnI3bpm5ULOfoy2ZZrJIC?page-id=88%3A11765&node-id=110-2717&starting-point-node-id=110%3A2717&scaling=scale-down)

## 文件分页与设计源

当前文件按以下九页组织。旧整屏原型和无引用重复组件已清理，不再保留「现状／改版／双主题副本」分页。

| 页面 | 内容 |
| --- | --- |
| [00 · 导览](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=127-2) | 页面索引、四个演示入口和维护规则 |
| [01 · 全局演示](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-2) | 跨业务流程；主画面及浮层均引用共享设计源 |
| [02 · 设计基础](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=126-2) | 锁定色板、字体、间距与三组双主题对照 |
| [03 · 组件库](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=125-155) | 通用控件、导航、图节点、业务面板与状态 |
| [10 · 语音工作流](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-5071) | 列表、构建、接入、测试、历史及响应式补充 |
| [20 · 语音智能体](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-6174) | 列表、创建、构建、接入、测试、历史及响应式补充 |
| [30 · 推理引擎](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-4800) | 引擎列表、管理面板、诊断状态与窄屏布局 |
| [40 · 资源管理](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-7157) | 后端智能体与向量检索 |
| [50 · 接入与运行](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-7432) | 运行入口和接入信息 |

业务页按主流程设计源、面板、状态、响应式补充和本业务演示分区。十五个主画面各有一个组件设计源；业务预览和全局演示均使用实例。演示目的地为同页顶层 Frame，浮层只包含遮罩及共享面板实例，不复制整屏背景。

| 画面 | 示例路由 | 设计源 | 全局演示画面 |
| --- | --- | --- | --- |
| 语音工作流 / 列表 | `/audio-workflows` | [98:5071](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-5071) | [110:2](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-2) |
| 语音工作流 / 构建 | `/audio-workflows/wf-1/build` | [98:5200](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-5200) | [110:137](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-137) |
| 语音工作流 / 接入 | `/audio-workflows/wf-1/access` | [98:5694](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-5694) | [110:883](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-883) |
| 语音工作流 / 测试 | `/audio-workflows/wf-1/test` | [98:5873](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-5873) | [110:1076](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-1076) |
| 语音工作流 / 历史 | `/audio-workflows/wf-1/history` | [98:6055](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-6055) | [110:1282](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-1282) |
| 语音智能体 / 列表 | `/voice-agents` | [98:6174](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-6174) | [110:1426](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-1426) |
| 语音智能体 / 创建 | `/voice-agents/new` | [98:6270](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-6270) | [110:1555](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-1555) |
| 语音智能体 / 构建 | `/voice-agents/agent-1/build` | [98:6355](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-6355) | [110:1664](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-1664) |
| 语音智能体 / 接入 | `/voice-agents/agent-1/access` | [98:6689](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-6689) | [110:2185](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-2185) |
| 语音智能体 / 测试 | `/voice-agents/agent-1/test` | [98:6859](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-6859) | [110:2370](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-2370) |
| 语音智能体 / 历史 | `/voice-agents/agent-1/history` | [98:7041](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-7041) | [110:2576](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-2576) |
| 推理引擎 / 列表 | `/engines` | [98:4800](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-4800) | [110:2717](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-2717) |
| 资源管理 / 后端智能体 | `/agent-backends` | [98:7157](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-7157) | [110:3040](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-3040) |
| 资源管理 / 向量检索 | `/vectors` | [98:7256](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-7256) | [110:3156](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-3156) |
| 接入与运行 / 概览 | `/runtime` | [98:7432](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=98-7432) | [110:3370](https://www.figma.com/design/qBnI3bpm5ULOfoy2ZZrJIC?node-id=110-3370) |

`wf-1`、`agent-1` 和引擎记录均为示例数据，实际页面使用服务返回的资源。全局演示保留全站浏览、工作流操作、智能体操作、引擎管理四个入口。

## 变量、组件与维护方式

- 颜色真源为 `src/web/src/styles/av.tokens.json`，`theme.css.ts` 负责映射既有语义角色。Figma Semantic 集合为 `VariableCollectionId:19:82`，白垣模式 `19:9`、苍渊模式 `19:10`；仅设计基础页的主题对照实例显式指定模式。
- 正文 14 px，常规／紧凑控件 40／32 px，圆角 6／8 px，间距按 8 px 基准组织。字体为 Inter、Noto Sans SC、Cousine。白垣主按钮为墨骨／石素，苍渊为初光／深渊。
- 组件库统一维护 Primary `19:134`、Secondary `19:151`、Input `19:172`、Status `19:175`、AppHeader `103:370`、DirectLink `49:10321`、QuickView `52:14403`、Editor `104:167`、Details `104:207`、Catalog `104:198`，并保留图节点与业务状态组件。
- 业务布局修改对应业务页的设计源，复用控件修改组件库，主题修改变量。演示实例只承载示例数据及必要的原型连接；不得以解绑实例、复制整屏或新增版本页代替同步。
- 每次变更先确认受影响节点，再按业务区域读取和修改；保留已有节点 ID。检查业务预览、全局演示和窄屏布局，核对组件引用、同页跳转、画布重叠及长文本。
- 所有画面由可编辑文字、自动布局、组件实例与矢量组成，不使用整页截图铺底。Figma 原型演示代表性流程，保存与请求结果为示例；实际生命周期以应用接口和测试为准。本项目没有自动将代码推送到 Figma 的同步服务。

## 推理引擎列表与诊断

### 密度与操作

`pages/EngineConsolePage.tsx` 和 `features/engines/console.css.ts` 负责引擎页本次调整，共享顶部导航与其他业务页不受影响。

- 引擎页顶部留白为 24 px；标题保留说明，移除装饰眉题。刷新、复检全部已启用引擎、注册引擎常显。
- 健康统计使用按内容宽度排列的 32 px 筛选按钮，数量不可用时显示「—」。异常数量大于零时才显示警示图标与加粗计数；选中项有背景和底部指示。
- 搜索、能力、排序与结果数量合并为列表工具栏，搜索宽度在可用空间内为 320–480 px。能力和排序使用紧凑固定宽度，不另设重复的状态下拉框。
- 桌面内容上限 1120 px，表头 32 px、普通行 64 px。名称占 24%，能力／状态／时间／操作列为 96／96／176／120 px，地址使用剩余空间。长名称和诊断消息可以增高。
- 编辑和复检保持一次点击可达，使用常显轻量文字按钮；名称打开详情。地址在表格内省略溢出内容，在详情中完整显示。
- 默认显示全部、按创建顺序排列。复检不自动重置筛选或改变排序。每页 50 条，接口分页读取完整目录后再执行搜索、筛选与统计；读取失败保留旧目录并暂停统计。
- ≤900 px 保留紧凑列表与全宽抽屉；≤600 px 搜索独占一行。窄屏操作目标为 40 px，保留名称、能力、状态和操作，地址与探测时间可进入详情查看。

### Figma 对应与示例交互

- 桌面设计源 `98:4800`、业务预览 `20:1370`、全局演示 `110:2717`；窄屏设计源 `100:7159`、预览 `68:13178`。设计源内有 19 条示例：健康 12、异常 4、停用 3。
- 状态筛选共用 Prototype 集合中的 `engineFilter/*` 变量，切换行可见性、选中指示和结果计数；零结果有清空筛选入口。
- 异常筛选中的 ASR-8 提供编辑和复检示例。全局演示中的编辑、复检中、复检失败浮层为 `145:5412`、`145:5488`、`145:5565`；本业务演示对应 `145:14217`、`145:14293`、`145:14370`。这些浮层引用现有面板组件，使用 ASR-8 的名称、Slug、地址和异常诊断信息。
- 原有注册、ASR-1 编辑、详情、引用阻断、模型／音色目录、保存失败与恢复示例继续通过共享面板呈现。原型搜索框和部分目录操作仅表达布局，不替代实际应用的完整数据交互。

### 保留的真实行为

注册与编辑继续通过 `EngineEditor` 执行字段校验、只 PATCH 变化项、未保存修改保护及失败保留；保存并复检失败不会撤销已保存配置。`EngineDetails` 按需读取诊断、引用、模型及音色目录，停用和删除前重新检查引用。单引擎操作按 ID 加锁，全量复检期间禁止配置写入。

快捷查看继续复用 `getEngine`／`probeEngine`，复检后重新读取资产；检查器、草稿和画布位置保持。模态支持键盘焦点管理和关闭后返回触发器。前端和本次调整均不显式加载 `.env`。

## 验证记录（2026-10-01）

本轮使用浏览器接口拦截提供 19 条混合状态记录，未写入真实服务，也未连接真实推理引擎。以下高度为浏览器 CSS 视口、100% 缩放；首屏计数只统计无诊断消息展开时完整可见的普通记录。

| 视口 | 首条数据顶部 | 普通行高 | 完整可见记录 |
| --- | --- | --- | --- |
| 1920 × 960 | 296.3 px | 64 px | 10 |
| 1440 × 900 | 296.3 px | 64 px | 9 |
| 1024 × 768 | 352.3 px | 64 px | 6 |
| 901 × 768 | 352.3 px | 64 px | 6 |
| 390 × 844 | 400.8 px | 148.5 px（窄屏） | 2 |

- Chromium 验证 1920×960、1440×900、1024×768、901×768、390×844、320×700，以及等效 200% 缩放的 960×480 CSS 视口，两主题共 14 组；无页面横向溢出。验证异常筛选、组合搜索／能力筛选、零结果恢复、直接编辑、Esc 关闭、焦点返回与停用复检禁用。
- 两主题另验证名称排序、长名称、长地址、旧探测时间、详情、复检失败与重试、全部复检、目录读取失败时旧数据与统计暂停，以及重新加载恢复。
- 新增混合状态筛选回归测试；引擎页 37 项测试通过。`pnpm --dir src/web test` 全量 16 个文件、172 项测试通过；`pnpm --dir src/web lint` 和 `pnpm --dir src/web build` 通过。构建保留既有 ELK 异步包超过 500 kB 的提示。
- Figma 核对桌面、窄屏、苍渊异常筛选和 ASR-8 诊断面板截图；受影响业务页及全局演示均无重叠、分区越界、失效引用或无效跳转；全局演示的 15 个主画面全部可达，保留 4 个流程入口。

旧迁移、顶部导航和快捷查看的历史验证记录保存在 Git 历史中；这里的节点表仅列当前有效设计源与入口。
