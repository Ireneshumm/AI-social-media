# 开新客户流程 / Client Onboarding

把这套自动发布系统接到一个新账号(你自己的、诊所第二个品牌、或帮客户管的账号)的标准流程。
**原则:每个客户一套独立的** —— 一个独立的 GitHub 仓库 + 这个客户自己的密钥 + 这个客户自己的素材文件夹。
客户之间完全隔离,内容、时间表、令牌互不干扰,一个出问题不会影响其他客户。

---

## 一、客户账号必须先满足的条件(硬性,绕不过)

Instagram 只允许 API 发到**专业账号**,所以每个新账号上线前必须:

1. 账号转成 **专业账号 / 商业账号(Professional / Business 或 Creator)**
   —— IG App 里:设置 → 账户类型与工具 → 切换为专业账号。免费、随时能转回去。
2. 这个 IG 账号**连接一个 Facebook 主页(Page)**。
3. 这个 Facebook 主页**授权给你的 Meta 开发者 App**(就是 `META_APP_ID` 那个 App)。
   —— 通常通过 Facebook 商务管理平台(Business Manager)把你加成主页管理员,
   或客户用 Facebook 登录授权你的 App。拿到授权后才能取得这个客户的 `PAGE_ACCESS_TOKEN`。

> 真正的私人账号(不愿意转专业号)任何工具都发不了,不是本系统的限制。

### 帮客户管的额外要求(很重要)
- **书面授权**:让客户书面同意你代为发布、以及授权你的 Meta App 访问他们的账号。
- **转发内容**:如果帮客户转发别人的视频(抖音/TikTok),版权和品牌风险由内容性质决定,
  上线前跟客户确认清楚只发他们有权发布的内容。

---

## 二、密钥分两类

### A. 每个客户都不一样(账号身份 + 内容源)—— 每开一个客户都要重新设

| 密钥 | 含义 | 从哪来 |
|---|---|---|
| `IG_USER_ID` | 客户的 IG 商业账号 ID | 用 `insights_check.py` / Graph API 查 |
| `PAGE_ACCESS_TOKEN` | 客户 FB 主页的长期令牌 | 客户授权你的 Meta App 后生成(60 天,到期要续) |
| `FB_PAGE_ID` | 客户 FB 主页 ID | 可留空,系统能自动推断 |
| `IG_LOCATION_ID` | 地理标签(可选) | `find_ig_location.py` 查客户所在地 |
| `ONEDRIVE_ROOT_PATH` | 这个客户素材的根目录 | 设成客户专属,如 `Clients/客户名/IG Auto Publisher` |
| `YTDLP_COOKIES` | 转发用的 cookies(可选) | 只有帮这个客户转发抖音/TikTok 时才需要 |

> 内容隔离的做法:`MS_*`(OneDrive 应用)可以共用你自己的,
> 只要给每个客户设一个**不同的 `ONEDRIVE_ROOT_PATH`**,
> 下面的 posts / stories / posted / drafts 子文件夹名可以保持一样。
> 这样所有客户的素材都在你自己的 OneDrive 里,但各自分开,互不混。

### B. 你的"服务基础设施"—— 所有客户共用,设一次即可

这些是你自己的账号/密钥,接新客户时**直接复制同样的值**过去:

- `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` —— 文案引擎(你的 key)
- `META_APP_ID` / `META_APP_SECRET` / `META_GRAPH_API_VERSION` —— 你的 Meta App
  (一个 App 可以管多个客户账号,只要每个客户都授权给它)
- `MS_TENANT_ID` / `MS_CLIENT_ID` / `MS_CLIENT_SECRET` / `ONEDRIVE_USER_EMAIL` —— 你的 OneDrive 应用
- `IMGBB_API_KEY` —— 图片托管
- `REPLICATE_API_TOKEN` —— 转发去字幕(可选)
- `SMTP_*` / `ALERT_EMAIL_*` —— 告警邮件(可选)
- 子文件夹名:`ONEDRIVE_POSTS_FOLDER_NAME` / `ONEDRIVE_STORIES_FOLDER_NAME` /
  `ONEDRIVE_POSTED_FOLDER_NAME` / `ONEDRIVE_DRAFTS_FOLDER_NAME` /
  `ONEDRIVE_FAILED_FOLDER_NAME` / `REPURPOSE_TARGET_FOLDER` —— 每个客户保持一样即可

> `OPENAI_*` 目前是备用(额度用完了),可以先不设。
> `DISPATCH_PAT` 用于"立刻发",现在已关掉立刻发,可以不设。

---

## 三、开一个新客户的步骤

1. **复制仓库**:把本仓库复制成一个新的 GitHub 仓库,命名如 `ai-social-<客户名>`。
   (GitHub 上 "Use this template" / 或新建仓库后把代码推上去。)
2. **完成第一节的账号条件**:客户转专业号 + 连 FB 主页 + 授权你的 Meta App。
3. **取这个客户的身份值**:
   - 生成客户的 `PAGE_ACCESS_TOKEN`(长期令牌);
   - 跑 `insights_check.py` 确认拿到 `IG_USER_ID`;
   - (可选)跑 `find_ig_location.py` 拿 `IG_LOCATION_ID`。
4. **在新仓库里设密钥**(Settings → Secrets and variables → Actions):
   - A 类:填这个客户的值;
   - B 类:复制你现有的服务值。
   - **令牌只进 GitHub Secrets,永远不要贴进聊天或代码。**
5. **建这个客户的素材文件夹**:在 OneDrive 的 `ONEDRIVE_ROOT_PATH` 下
   建 posts / stories / posted / drafts 文件夹,放进客户的素材。
6. **验证**:在新仓库手动触发一次 post-publisher(workflow_dispatch),
   确认能发出一条;再看一次发布顺序/题材轮换正常。
7. **上线**:确认各 workflow 的 cron 时间表符合这个客户的需求(见下)。

---

## 四、上线后

- **发布节奏**:时间表在各 workflow 的 `cron` 里(UTC)。每个客户可以不一样。
  默认 feed 每天 7 条、story 每小时轮询发、转发每 20 分钟重试队列。
- **令牌续期**:`PAGE_ACCESS_TOKEN` 约 60 天到期。`token_maintenance.py` 会在快到期时告警。
  每个客户的令牌要各自续。
- **监控**:开 `ALERT_EMAIL_*` / `SMTP_*` 后,出错会发邮件提醒。

---

## 五、一句话总览

> 接一个新客户 = **复制一套仓库** + 填 **A 类(客户身份 + 素材根目录)** + 复制 **B 类(你的服务密钥)** + 建**客户专属素材文件夹**。
> 代码一行都不用改。
