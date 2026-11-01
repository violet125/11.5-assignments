# 接口说明

所有接口返回 JSON。写操作需要角色标识，通过请求头 `X-Actor-Role` 传入，可选操作人名 `X-Actor`。

角色取值：`business`（业务/招商）、`engineer`（工程）、`reviewer`（复核）、`admin`（管理员）。

## 错误格式

```json
{ "error": "错误描述" }
```

| 状态码 | 含义 |
| --- | --- |
| 400 | 输入不合法、缺少角色标识 |
| 403 | 角色无该操作权限（如业务角色解除专业确认阻断） |
| 404 | 资源不存在 |
| 409 | 当前状态不允许该操作（如已交接后再次提交） |
| 502 | 外部模型服务调用失败 |

---

## GET

### GET /api/config

前端启动配置：字段字典、取值状态标签、用途规则、角色权限、人工核实项、AI 是否启用；另含 `return_reasons`（12 个退回原因代码 → 文案的字典，供前端驳回下拉使用）。

### GET /api/spaces

当前房源台账。

```json
{
  "spaces": [
    {
      "name": "房源A",
      "values": {"area": 500, "rent": 55, "power": 100, "load": 300, "height": 3.6,
                 "layer_height": null, "redundancy": null},
      "trace": {"source": "图纸", "verifier": "张工", "verified_at": "2026-01-15", "version": "v1"},
      "meta": {"park": "示例园区", "building": "A 栋"},
      "trace_complete": true
    }
  ],
  "count": 1
}
```

### GET /api/spaces/template

下载用台账模板（一个字段齐全的虚构样例）。

### GET /api/cards

需求卡列表。可选查询参数：

| 参数 | 说明 |
| --- | --- |
| `status` | 按状态筛选（draft / pending / handover / returned / escalated） |
| `q` | 关键词，匹配标题或客户（模糊包含） |
| `page` | 页码，从 1 开始；不传则不分页 |
| `page_size` | 每页条数，1 ~ 100；传 `page` 时默认按传入值分页 |

传了分页参数时响应含 `total`、`page`、`page_size`。

### GET /api/card?card_id=xxx

需求卡详情。含 `items`、`validation`、`reviews`、`confirmations`、`review_history`、`field_dictionary`、`visible_fields`。

### GET /api/card/report?card_id=xxx

导出 Markdown 报告。响应 `{filename, markdown}`。报告含字段裁定、交接校验、复核与专业确认、空间匹配（基于当前台账重算）与报价单。需 `report.export` 权限。

### GET /api/quotes?card_id=xxx

报价单列表，可按需求卡过滤。

```json
{
  "quotes": [
    {
      "id": "quote_xxx",
      "quote_no": "RJ-202611-001",
      "quote_version": 1,
      "round": "首次报价",
      "card_id": "card_xxx",
      "card_title": "设备研发厂房需求",
      "card_version": 3,
      "space_name": "房源A",
      "terms": {"unit_price": 42, "unit_label": "元/㎡/月", "area": 1200, "area_label": "㎡",
                "rentable_area": 900, "monthly_total": 37800, "deposit_months": 3,
                "free_rent_days": 30, "lease_term_months": 36, "escalation_pct": 0,
                "property_fee": 8, "validity_days": 15,
                "renovation_input": 315000, "subsidies_total": 0,
                "rent_3y": 1360800, "property_3y": 259200,
                "total_3y": 1935000, "total_revenue": 1360800,
                "ledger_rent": 45, "discount_pct": 6.7,
                "requires_approval": true, "assumptions": ["..."], "note": ""},
      "match_conclusion": "已填条件满足",
      "status": "draft",
      "revision_reason": "",
      "valid_until": 1796160000,
      "approved_by": null,
      "approved_at": null,
      "actor": "李经理",
      "actor_role": "business",
      "created_at": "2026-11-01T08:00:00Z"
    }
  ]
}
```

`status` 取值：`draft`（草稿）、`issued`（已发出）、`voided`（已作废）。`valid_until` 为报价有效期止的 Unix 时间戳（生成时间 + `validity_days` 天）。`approved_by`/`approved_at` 为管理员授权审批留痕，非空表示已获授权。

### GET /api/audit?card_id=xxx

审计留痕。不传 `card_id` 返回全量最近记录。可选参数 `limit`（默认 200）。

响应含 `chain_valid`：对全表按 `sha256(prev_hash + 规范化 JSON)` 重算校验的结果（`true`/`false`）。迁移前的历史记录（无 hash）不参与校验，不影响结果。

### GET /api/metrics

过程计数：需求卡数、复核动作数、驳回数、升级数、专业确认数、台账房源数、溯源完整房源数、存在阻断的需求卡数、报价单总数与已发出数。

响应含 `caveat` 字段说明这些计数不等于业务交接遗漏率。

---

## POST / PUT

### POST /api/extract

```json
{ "text": "客户访谈原文" }
```

响应：

```json
{
  "items": {
    "area": {"field": "area", "state": "specified", "value": 600.0, "unit": "㎡",
             "evidence": "面积至少600平方米", "origin": "rule", "note": ""}
  },
  "mode": "规则演示模式：未连接大模型，仅识别固定表达",
  "source": "原文",
  "state_labels": {"specified": "已明确", "...": "..."},
  "redacted": false
}
```

`redacted` 为 true 表示原文含敏感信息，已脱敏后送模型。

### POST /api/cards

新建需求卡。

```json
{
  "title": "设备研发厂房需求",
  "customer": "某设备公司",
  "purpose": "研发办公",
  "source": "访谈原文",
  "items": []
}
```

需 `card.create` 权限（业务/复核/管理员）。

### PUT /api/card

更新需求卡。`items` 可选。

```json
{
  "card_id": "card_xxx",
  "title": "新标题",
  "purpose": "仓储物流",
  "items": [
    {"field": "area", "state": "specified", "value": 600, "evidence": "面积至少600平方米"}
  ]
}
```

`items` 中每项 `state` 取 `specified` / `unknown` / `not_requested` / `none_confirmed`。状态非 `specified` 时不得带 `value`。

### POST /api/card/validate

只跑交接校验，不改状态。

```json
{ "card_id": "card_xxx" }
```

响应：

```json
{
  "validation": {
    "passed": false,
    "blocking_count": 2,
    "advisory_count": 1,
    "issues": [
      {"level": "blocking", "type": "no_professional_confirm", "field": "height",
       "message": "最低净高已填写但需专业确认，尚未由具备权限的角色确认实测口径",
       "question": "请工程或复核角色确认最低净高的实测口径与数值。"}
    ],
    "checklist": ["补问列表"],
    "required_fields": ["area", "rent", "power", "height"],
    "professional_fields": ["height", "load"],
    "confirmed_fields": []
  },
  "summary": "校验未通过：2 项阻断待处理"
}
```

`issue.type` 取值：`missing_value`、`missing_evidence`、`no_professional_confirm`、`unit_conflict`、`unknown_purpose`、`not_requested`。

### POST /api/card/submit

提交交接。校验不通过时不改状态，返回 `submitted: false` 与补问清单。

```json
{ "card_id": "card_xxx", "complete": false }
```

`complete` 为 true 表示直接完成交接（状态 `handover`），否则进入 `pending`。

响应：`{submitted, status, validation, summary, card}`。

### POST /api/card/review

执行复核动作。

```json
{
  "card_id": "card_xxx",
  "action": "reject",
  "reason": "面积口径不明",
  "detail": {"reason_code": "MISSING_BASIS", "resolution": "补客户原话"}
}
```

`action` 取 `accept` / `change_level` / `change_explanation` / `reject` / `escalate`。

各动作的 `detail` 要求：

| 动作 | detail 必填 |
| --- | --- |
| accept | 无 |
| change_level | `field`、`level`（blocking/advisory/info） |
| change_explanation | `field`、`explanation` |
| reject | `reason_code`（必须取退回原因枚举，见下表） |
| escalate | `escalate_to` |

除 accept 外均需 `reason`。需 `card.review` 权限。

驳回原因枚举（`GET /api/config` 的 `return_reasons` 同步下发）：

| 代码 | 文案 |
| --- | --- |
| HW_MISSING | 硬件条件缺失（层高/承重/配电等未登记） |
| HW_MISMATCH | 硬件条件不匹配 |
| HW_UNVERIFIED | 硬件条件未核实 |
| CAPACITY_MISSING | 容量参数缺失（供电容量等） |
| AREA_MISMATCH | 面积不匹配 |
| DELIVERY_UNCONFIRMED | 交付条件未确认 |
| PRICE_OVER_LIMIT | 价格超限 |
| INCENTIVE_ABOVE_AUTH | 让利超授权 |
| VERSION_CONFLICT | 版本冲突 |
| OLD_MATERIAL_SENT | 送错材料/旧版材料 |
| MISSING_BASIS | 缺少依据或溯源 |
| SYSTEM_UNAVAILABLE | 系统不可用（人工通道） |

### POST /api/card/confirm

专业确认。

```json
{ "card_id": "card_xxx", "field": "height", "note": "实测4.2米" }
```

仅工程/复核/管理员可调用。业务角色返回 403。

### POST /api/spaces

导入房源台账（整份覆盖）。

```json
{
  "spaces": [
    {"name": "房源A", "area": 800, "rent": 40, "power": 200, "load": 600, "height": 4.5,
     "source": "图纸", "verifier": "张工", "verified_at": "2026-01-01", "version": "v1",
     "efficiency_ratio": 75, "property_fee": 8, "delivery_status": "L1",
     "net_height": 3.3, "power_redundancy": "双路", "hvac_overtime_fee": 2}
  ]
}
```

除匹配参数外，可另填六个报价参数（不参与匹配，仅随报价单带出）：
`efficiency_ratio`（得房率 %，1~100）、`property_fee`（物业费 元/㎡/月，0~1000）、
`delivery_status`（交付标准：毛坯/L1/L2/L3）、`net_height`（净高 米）、
`power_redundancy`（单路/双路/双路+应急/N+1）、`hvac_overtime_fee`
（空调加时费 元/㎡/月）。超出范围或非法枚举返回 400。

校验失败返回 400 并说明原因（单位混填、缺名称、日期格式、重复名称等）。响应含 `count` 与 `issues`（非阻断的整体提示）。

### POST /api/match

执行硬条件匹配。

```json
{
  "items": [{"field": "area", "state": "specified", "value": 500}],
  "spaces": null
}
```

`spaces` 不传则使用当前台账。只有 `state: "specified"` 的项参与匹配。

响应：

```json
{
  "results": [
    {
      "name": "房源A",
      "status": "已填条件满足",
      "checks": [{"field": "area", "label": "最小面积", "unit": "㎡", "required": 500,
                  "actual": 800, "status": "满足", "basis": "需 ≥ 500㎡，实际 800㎡"}],
      "trace": {"source": "图纸", "...": "..."},
      "trace_complete": true,
      "gaps": []
    }
  ],
  "missing_requirements": ["最高月单价"],
  "manual_checks": ["行业准入与消防条件", "..."],
  "matched_count": 1,
  "unverified_count": 0,
  "failed_count": 0
}
```

`status` 取值：`已填条件满足`、`条件满足但溯源不全`、`待核实`、`不匹配`。

### POST /api/card/delete

删除需求卡。仅管理员（需 `*` 权限）。

### POST /api/quotes

生成报价单草稿。需 `quote.create` 权限（业务/管理员）。

```json
{
  "card_id": "card_xxx",
  "space_name": "房源A",
  "revision_reason": "客户要求缩短免租后重新报价",
  "overrides": {"unit_price": 45, "deposit_months": 6, "free_rent_days": 15,
                "lease_term_months": 24, "property_fee": 8, "escalation_pct": 3,
                "renovation_cost_per_sqm": 450, "renovation_subsidy": 100000,
                "relocation_subsidy": 50000, "validity_days": 30,
                "note": "季付优惠"}
}
```

服务端按需求卡当前字段对当前台账重跑硬条件匹配，只有匹配结论为
"已填条件满足"或"条件满足但溯源不全"的房源可以生成（否则 409）。
条款基准取台账登记的租金（元/㎡/月）与面积；台账未登记时必须通过
`overrides` 人工填写，否则 400。条款上下限：押金 0~12 个月、免租 0~365 天、
租期 1~120 个月、物业费 0~1000 元/㎡/月、年递增 0~50%、得房率 1~100%、
有效期 1~180 天、装修单价 0~10000 元/㎡、补贴 0~1 亿元。

版本与轮次：编号 `RJ-{YYYYMM}-{序号}` 全局递增；同一需求卡 + 房源再次生成时
自动升版（`quote_version` +1，`round` 变为"修订报价"），此时必须提供
`revision_reason`（≥4 字）否则 400；上一版（非作废）自动置 `voided`，响应
`quote.superseded` 列出被作废的旧版 id。

服务端自动计算：`rentable_area`（登记面积 × 得房率，台账缺得房率按 100%）、
`rent_3y`（三年租金含年递增）、`property_3y`（三年物业费）、
`renovation_input`（按交付标准查表估算或人工覆盖）、`total_3y`
（三年总占用成本 = 三年租金 + 三年物业费 + 装修投入 − 补贴）、
`discount_pct`（相对台账租金的单价折扣）。台账未登记物业费时按 0 计入并在
`terms.assumptions` 提示。

响应：`{quote, caveats}`。`caveats` 为必须随附的口径提示。

### POST /api/quote/status

报价单状态流转。需 `quote.create` 权限。

```json
{ "quote_id": "quote_xxx", "action": "issue" }
```

`action` 取 `issue`（草稿→已发出）或 `void`（作废，草稿/已发出均可）。非法流转返回 409。

`issue` 有两道门槛（均返回 409）：

- **让利授权**：`terms.requires_approval` 为真（免租超 `quotation.authority.max_free_rent_days`（默认 92 天）或单价折扣超 `max_discount_pct`（默认 5%））且报价单尚无管理员审批记录时，不能发出；须管理员先调用 `POST /api/quote/approve`。
- **专业确认附页**：需求卡 `specified` 字段中含承重（load）或供电容量（power）时，对应字段必须已有有效专业确认，否则不能发出。

### POST /api/quote/approve

超授权报价的授权审批。需 `quote.approve` 权限（仅管理员）。

```json
{ "quote_id": "quote_xxx" }
```

仅 `draft` 状态可审批；记录审批人与审批时间（`approved_by`/`approved_at`）并写入审计。业务/工程/复核角色调用返回 403。

### POST /api/assistant/chat

智能助手对话。只读能力：不修改业务数据、不写审计留痕。请求需带角色标识。

```json
{ "message": "生成补问清单", "card_id": "card_xxx" }
```

- `card_id` 可空；提供时服务端组装该卡的字段裁定、交接校验、报价单与当前台账匹配结论作为上下文（匹配基于当前台账重算）。
- 响应：`{ "reply": "…文本…", "mode": "rule" | "ai" }`。
- 已配置 `AGENT_API_KEY` + `AGENT_MODEL` 时走 AI 模式（发送内容先脱敏，模型失败自动回落规则模式）；未配置时为本地规则助手，按固定意图生成补问清单、需求总结、报价口径、字段口径与匹配结论解读。
- 校验：消息为空返回 400；`card_id` 不存在返回 400（需求卡不存在）。

