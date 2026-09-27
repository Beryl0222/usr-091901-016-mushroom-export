# 野生菌出口时效链

面向香格里拉高海拔鲜松茸出口的时效协同后端：把**采集点 → 菌种鉴别 → 农户交售 → 加工分级 → 箱码 → 属地查检 → 证书签发 → 口岸预约 → 冷链出境**连成一条可追溯、可重判、按角色隔离的链。日本、德国、越南等目的地的检疫准入要求按规则版本管理，鲜品有鲜度时钟，拒收/退运后可精确回溯到采集批次与应付农户。

运行检查：

```bash
python3 service.py --check          # 契约与命令一致性检查
python3 service.py --port 8000      # 启动服务
python3 -m unittest -v              # 全部测试（33 项：领域 29 + HTTP 4）
```

接口：

| 接口 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 运行状态 |
| `/contract` | GET | 领域契约（参与者、状态、命令、不变量） |
| `/api/commands` | POST | 命令写入，返回事件；需携带调用方身份 `as` |
| `/api/views` | POST | 视图读取（box / settlement / impact / rules），按角色遮蔽 |

## 领域模型

```
farmer（菌农）
  └─ collection（采集批次：采集点/菌种/数量/采集时刻/鲜度窗口）
       └─ identify_species（属地海关鉴别）
            └─ batch（交售批次：企业、单价、买方合同[仅企业可见]）
                 └─ box（箱码：形态 鲜品/冷冻/干制、等级、来源清单 sources）
                      ├─ split_box / merge_boxes（拆批/合箱，数量守恒）
                      ├─ convert_form（转冷冻/干制，鲜度时钟停止）
                      ├─ shipment（出运计划：目的地、计划起飞时间、版本）
                      ├─ inspection（查验：自然键去重）
                      ├─ certificate（放行结论：绑定规则版本与出运时点）
                      ├─ booking（口岸预约/改约）
                      └─ rejection（拒收/退运：按来源比例分摊）
```

每个箱持有 `sources: [{batch_id, quantity}]`，这是全部追溯与分摊的唯一根；拆批按比例切分、合箱按批次相加、形态转换不改变来源。

## 关键业务语义（见 domain_contract.json invariants）

1. **来源守恒**：拆批、合箱、转冷冻/干制后仍可追到原始采集批次、鉴别结论与农户；`conservation_check` 校验活箱来源引用量等于批次累计装箱量。
2. **规则按出运时点选版本**：目的地规则为 `[effective_from, effective_to)` 左闭右开时段；放行证书记录签发时命中的规则版本与出运计划版本。计划起飞时间或目的地一变，重新选版本。
3. **事件驱动失效，旧结论不得沿用**：证书补正、口岸改约、航班延误、温控异常、形态转换、拆批/合箱、查验不合格都会使当前放行证书失效（`release_invalidated` 事件留痕，状态退回待查验）。补正材料齐全或异常处置完成**不**自动恢复旧证书，必须重新评估签发。
4. **查验自然键去重**：自然键 =（箱, 查验类别, 当地日期）。属地海关与口岸海关对同一次查验的重复上报只产生一条记录，两个机构与上报人均留痕（`inspection_deduplicated`）；跨天即为新查验。
5. **按角色遮蔽**：企业只见本企业货物，买方合同只出现在所属企业的 `commercial` 字段；查验人员可读溯源/查验/规则但看不到合同与结算价；菌农只见本人交售与应付款；`impact` 视图中农户应付款对查验人员遮蔽。
6. **拒收精确分摊**：支持部分拒收；拒收量沿来源结构（含拆批链）按比例分摊到采集批次与农户，`impact` 视图给出各采集批次受影响数量与各农户付款扣减，`settlement` 视图据此计算应付。
7. **鲜度窗口**：鲜品从最早采集时刻起算（取来源中最短鲜度时长），视图实时给出剩余小时数；起飞时点剩余鲜度低于目的地要求、或窗口越界均阻断放行；转冷冻/干制后 `clock=stopped`，按对应形态规则判定。

## 可行性判定（evaluate_release / box 视图）

判定按**计划出运时间**（而非当前时间）选规则，硬性阻塞 `blockers` 包括：无出运计划/目的地无生效规则、鲜度越界或起飞时鲜度不足、未处置温控异常或峰值超限、等级低于要求、缺属地参与的合格检疫查验、无有效口岸预约或预约晚于起飞、已拒收等。原放行结论失效只作为 `notices` 提示——重新签发正是解除动作。

`box` 视图还返回 `alternative_routes`：其他口岸有余位且赶得上起飞的时段及各自阻塞。

## 命令示例

```bash
# 企业规划出运（as 为调用方身份，企业命令的主体必须与身份一致）
curl -s localhost:8000/api/commands -H 'Content-Type: application/json' -d '{
  "command": "plan_shipment",
  "payload": {"enterprise_id":"e1","box_id":"B1","destination":"日本",
              "planned_departure":"2026-09-11T12:00Z","route":["KMG"],"flight_no":"CA999"},
  "as": {"kind":"enterprise","enterprise_id":"e1"}}'

# 调度员查看一箱货：鲜度、阻塞、规则、替代路线
curl -s localhost:8000/api/views -H 'Content-Type: application/json' -d '{
  "view":"box","payload":{"box_id":"B1","now":"2026-09-10T20:00Z"},
  "as":{"kind":"dispatcher"}}'
```

角色：`dispatcher`（调度/平台协同）、`enterprise`、`officer`、`farmer`、`carrier`。完整命令清单见契约 `commands`，每个命令的角色授权见 `service.py` 的 `COMMAND_ROLES`。

## 文件

- `domain_contract.json` — 领域契约：参与者、状态、形态、命令、视图、不变量
- `domain.py` — 领域层：`ChainStore` 聚合根（仅标准库、线程安全）、命令分发、守恒自检
- `service.py` — HTTP 入口与角色鉴权
- `test_domain.py` — 29 项领域不变量测试（以出口日本/德国为主线）
- `test_service.py` — 契约与 HTTP 端到端测试
