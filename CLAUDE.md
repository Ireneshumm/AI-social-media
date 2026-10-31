# CLAUDE.md

本仓库是 Reborn Aesthetics / Dr John Li 的**全自动社媒发布系统**(Python + GitHub Actions 定时任务;素材放 OneDrive;通过 Meta Graph API 发到 Instagram + Facebook;AI 写文案;主题轮换、Stories、TikTok 转发、合规过滤、令牌维护)。

---

## 🤖 AI 员工(Subagents)

本仓库在 `.claude/agents/` 安装了一组专职"AI 员工"。派活时**按下面的对照表自动选择**合适的员工——符合条件就直接派,不用每次来问我。一个任务可以同时派多个员工。

### 员工名单(每人一句话职责)

| 员工 | 文件 | 一句话职责 |
|---|---|---|
| **代码审查员** Code Reviewer | `engineering-code-reviewer.md` | 审查改动的正确性、安全、可维护性、性能,给出 🔴阻断/🟡建议/💭小问题 分级意见 |
| **AI 代码安全审计员** AI-Generated Code Security Auditor | `security-ai-generated-code-auditor.md` | 专查 AI 写的代码里泄露的密钥、权限/注入漏洞；**只读、从不打印密钥值** |
| **极简改动工程师** Minimal Change Engineer | `engineering-minimal-change-engineer.md` | 只做"解决问题所需的最小改动",拒绝顺手重构和扩大范围 |
| **后端架构师** Backend Architect | `engineering-backend-architect.md` | Python 功能开发 + 第三方 API 集成(Meta/微软 Graph/OpenAI 等)的重试、幂等、错误处理、可靠性 |
| **DevOps 自动化工程师** DevOps Automator | `engineering-devops-automator.md` | GitHub Actions 工作流、定时任务(cron)、CI 流程 |
| **法务合规检查员** Legal Compliance Checker | `support-legal-compliance-checker.md` | 法律 / 隐私 / 支付 / 广告合规审查(**按澳洲法规**,见下) |

### 自动派活规则(任务 → 员工)

- 改 `.github/workflows/*.yml`、发布时间表、cron、CI → **DevOps 自动化工程师**
- 写/改 Python 业务逻辑,或对接 Meta / 微软 Graph / OpenAI / WordPress / imgbb 等 API → **后端架构师**
- 修 bug、小调整、只动一两行 → **极简改动工程师**
- 任何改动在合并前的把关、PR 审查 → **代码审查员**
- 涉及密钥 / 令牌 / 凭证,或新写的 AI 生成代码 → **AI 代码安全审计员**(复核)
- 发帖文案是否合规、医美/医疗广告(AHPRA)、隐私、支付、用户条款 → **法务合规检查员**
- 典型组合:后端架构师**写** → 代码审查员 + AI 代码安全审计员**复核** → 极简改动工程师收尾。

### 铁律(所有员工和 Claude Code 都必须遵守)

1. **绝不打印、提交、写入任何密钥、令牌或密码。** 凭证只存在 GitHub Secrets 里;代码、日志、PR、commit 信息中一律不得出现其明文值。
2. **不删除、不轮换我的任何凭证**(Meta 令牌、微软 Graph 凭证、SMTP 等)。若发现凭证疑似泄露或过期,**只"报告 + 建议"**,由我本人决定是否轮换。
3. **只改任务需要改的地方。** 不顺手重构、不加"以防万一"的代码;发现额外问题就单独列为待办,不塞进本次改动。
4. **大改动先问我**(架构级改动、删文件、调整发布节奏、改动合规逻辑、改工作流里的凭证/权限等),拿到我确认再动手。
5. **只提 PR,不直接改 `main`。**

### 法域(重要)

我在**澳大利亚**经营。凡涉及**法律、隐私、支付**的规定,一律**按澳洲法规**执行,包括但不限于:

- 医疗 / 医美广告:**AHPRA**(澳洲医疗从业者监管局)广告规范,以及 **TGA** 治疗用品广告准则
- 隐私:**Privacy Act 1988 / 澳洲隐私原则(APPs)**
- 消费者 / 广告:**Australian Consumer Law (ACL)**
- 支付:**PCI-DSS**

不要默认套用美国 / 欧盟 / 中国的规则。GDPR、CCPA、中国广告法等**仅作参考**,最终以**澳洲法规为准**;两者冲突时以澳洲为准。

### 共享工作日志

**每完成一项工作,在 PR 描述的最后追加一行记录**,格式(竖线分隔):

```
日期 | Claude Code | 仓库名 | 员工 | 做了什么 | 结果 | 待我决定的事
```

示例:

```
2026-10-31 | Claude Code | ai-social-media | DevOps 自动化工程师 | 安装 6 个 AI 员工 + 新建 CLAUDE.md | 已提 PR 待审 | 是否再补装隐私官 / AI 工程师
```
