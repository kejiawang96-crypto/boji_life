# AI Health Log

[English](README.md)

个人健康数据中心：一个私有 Git 仓库统一保存训练、饮食、睡眠和身体指标，
任何 AI 助手都能读取完整上下文并持续记录，不用反复介绍自己。

由 [ai-health-log 模板](https://github.com/ZhenhaoLi00/ai-health-log) fork 而来，
扩展了更丰富的训练表格、渐进对比、能量账本、睡眠分析和图表。

> **隐私优先：** 这是一个**模板**。请用它创建自己的仓库
> （GitHub → *Use this template*），并保持**私有**——它将保存你的真实身体数据。
> 不要提交 API 密钥或令牌，详见 [SETUP.md](SETUP.md)。同步脚本会检查所有
> GitHub 推送目标，只有确认是私有仓库才允许推送。

## 让 AI 帮你完成初始化

将下面的**英文提示词原样复制**给 ChatGPT、Claude、Codex、Muse 等 AI 助手。
它会引导你完成整个配置过程。

---

I want to set up my own AI health log from this template.

Please:
1. Read README.md, README.zh-CN.md, AGENTS.md and SETUP.md in the template repo first.
2. Ask me for: my timezone, height, current weight, primary goal (fat loss /
   muscle gain / maintenance), daily protein and calorie targets (suggest them
   if I don't know), training frequency, and which training app I use
   (e.g. SynFit (训记), Apple Health, or manual logging).
3. Walk me through each step: creating a PRIVATE repo from the template,
   filling in profile.md, getting my training app's API key and storing it
   outside the repo (chmod 600), configuring git access (deploy key or PAT),
   running scripts/health-daily-sync.py, and scheduling it to run daily
   (GitHub Actions workflow included).
4. Follow the repo conventions: never commit secrets, cache API responses by
   date, keep estimates labeled as estimates, and always show me a change
   summary and wait for my confirmation before writing anything back to a
   third-party API.

---

## 数据来源

| 来源 | 内容 | 方式 |
|---|---|---|
| 训记 / Xunji (SynFit) Open API | 力量训练记录 | `scripts/health-daily-sync.py`，经 GitHub Actions 每天同步 |
| Apple HealthKit | 运动、睡眠 | 助手的健康连接器 → 写入 daily |
| 手动 | 饮食（拍照估算）、晨起空腹体重、腰围、备注 | 直接告诉助手 |

先填写 `profile.md`：身高、体重、年龄、目标、蛋白质/热量目标、训练计划
和一个保守的 BMR。

## 仓库结构

```
README.md / README.zh-CN.md  本文档
SETUP.md                     初始化指南
AGENTS.md                    AI 助手编辑本仓库的约定
SUMMARY.md                   总览（自动生成，不要手动改）
profile.md                   目标、指标、基线（先填这个）

data/daily/YYYY-MM-DD.md     每日日志——所有人可读的内容都在这里
data/daily/EXAMPLE.md        格式示例（假数据）
data/workouts/               训记 API 原始缓存，按日期一个 JSON（机器用）

reports/weekly.md            本自然周（周一到周日，自动生成）
reports/monthly.md           本自然月（自动生成）
reports/weight-trend.png     空腹体重曲线 + 目标线
reports/sleep/               30 天睡眠图表：分期、质量、体征

scripts/health-daily-sync.py 同步训记 → 每日日志，重算报告并推送
scripts/health-energy-sync.py 每日能量账本
scripts/health-sleep-sync.py  Apple HealthKit 睡眠 → 每日日志
```

## 每日日志

每天一个文件 `data/daily/YYYY-MM-DD.md`，包含以下小节：

- `## Sleep`——人类可读小结 + 隐藏的 `<!-- SLEEP-DATA {...} -->` 机器数据
  （分期、质量、效率、体征）
- `## Meals`——首行 `Day total:`，每餐一个 `###` 小节。
  拍照估算一律标 `estimated`，只有营养标签或用户明确给出的数值才标 `verified`
- `## Training`——训记自动管理区块（见下）
- `## Body metrics`——晨起空腹体重（每天）、腰围（每周）
- `## Energy balance`——摄入 vs 支出账本（见下）
- `## Notes`

### 训练记录格式

每个动作一张 markdown 表格，每组单独一行、不合并，重量用 API 原生单位
（训记是 lb）：

**Bench press**

| Set | Weight | Reps | Last 12-28 weight | Last 12-28 reps |
|---|---|---|---|---|
| 1 | 135 lbs | 10 | 135 lbs | 8 |
| 2 | 135 lbs | 10 | 135 lbs | 8 |

如果该动作之前练过，表格右侧会并排显示上次的每组数据，下方还有一行
差值小结：

`vs last 12-28: volume +620 lb (+21.7%) · top weight +20 lbs · total reps +6`

（容量 = Σ 重量 × 次数。）

每次训练还会按举起容量粗估热量。

### 能量账本

`scripts/health-energy-sync.py` 维护每日账本：

- **支出** = BMR × 1.2（久坐基线，含日常活动）+ 运动消耗
- **运动热量**：优先用实测值（如 Apple Watch）；训记容量估算只做 fallback，
  永不重复计算
- **趋势预测**：每日缺口 × 7 / 7700 = 按此节奏预计的 kg/周

每次记录饮食后重算。

## 报告

- **周报**（`reports/weekly.md`）：自然周（周一到周日）截至今天——训练容量、
  饮食、体重趋势、能量账，以及**睡眠分析**（本周睡眠 vs 基准：
  成人 7–9 小时、深睡 13–23%、REM 20–25%、效率 ≥ 85%）。
- **月报**（`reports/monthly.md`）：自然月截至今天。
- 图表：`reports/weight-trend.png`（空腹体重 + 目标线）和 `reports/sleep/`
  （30 天分期/质量/体征）。

## 自动化

`.github/workflows/daily-sync.yml` **每天**运行
`scripts/health-daily-sync.py`（默认约 22:30 `America/Chicago`——请按你的时区
调整 cron 和 `HEALTH_LOG_TZ`）：拉取 → 抓取昨天+今天的训记数据
（`--force-refresh`，其余按日期缓存）→ 更新每日日志 → 重算 `SUMMARY.md`、
周报/月报和体重图 → 有变化才提交推送。也可手动触发（`workflow_dispatch`）。

## AI 助手约定

1. **先读后写。** 动笔前先看 `profile.md`、`AGENTS.md` 和今天的
   `data/daily/` 日志。
2. **一天一个文件。** 所有人可读的内容进 `data/daily/YYYY-MM-DD.md`。
   `data/workouts/` 是机器缓存——可以读，不要手动改。
3. **别碰托管区块。** `## Training` 里的 `XUNJI SYNC` 标记区块归同步脚本
   所有；标记外的原样保留，标记本身永不手动改动。
4. **估算 vs 实测。** 拍照估算的饮食永远标 `estimated`，绝不把估算当实测呈现。
5. **重算而非手改。** `SUMMARY.md` 和 `reports/` 下所有内容都是生成的。
6. **写回第三方 API 前先确认**：先展示变更摘要，等用户确认。
   读取和本地仓库更新不需要确认。
7. **绝不提交密钥。** API key 放在仓库外（见 SETUP.md）。

## 数据源

本模板自带训记（SynFit）集成。这个结构也适用于手动记录或其他训练 App——
让你的 AI 调整 `scripts/health-daily-sync.py`，或直接写入 `data/daily/`。

## License

[MIT](LICENSE) 覆盖模板软件与文档，**不包括你的私有健康数据**。
个人记录不得再分发。
