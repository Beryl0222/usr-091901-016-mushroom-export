# 野生菌出口时效链

连接山区采集、菌种鉴别、加工批次、检疫证书、口岸预约和冷链运输。项目以领域契约约定参与者、状态和不可破坏的业务原则，时效协同后端围绕同一语义提供溯源、放行评估与调度视图。

## 运行

- `python3 service.py --check` 核对服务配置与目的地规则
- `python3 service.py --port 8000` 启动服务
- `python3 -m unittest -v` 运行全部测试

## 身份与可见性

接口通过 `X-Actor` 头声明身份，如 `enterprise:ENT1`、`inspector_local`（属地海关）、`inspector_port`（口岸海关）、`settlement`（结算）、`dispatcher`（调度）、`carrier`（承运）。

- 企业只见本企业资料；买方合同仅货主企业可见
- 查验人员按职责读取（属地查检/证书由属地海关登记，口岸查验由口岸海关登记）
- 农户结算视图只有交售与应付金额，不暴露买方合同

## 接口概览

| 接口 | 说明 |
| --- | --- |
| `GET /health` · `GET /contract` | 健康检查与领域契约 |
| `POST /batches` `/deliveries` `/lots` `/boxes` `/boxes/split` | 采集登记（含菌种鉴别）、农户交售、加工分级（鲜/冻/干转化）、装箱合箱、拆批 |
| `GET /boxes/{id}/trace` | 沿箱码折算回采集批次、采集点与农户 |
| `GET /boxes/{id}/status` | 调度视图：剩余鲜度窗口、当前阻塞、所用规则、替代路线 |
| `POST /inspections` | 查验上报（同一查验单号重复上报只记一次，结论冲突返回 409） |
| `POST /certs` · `POST /certs/correct` | 证书签发与补正（旧证作废） |
| `POST /bookings` · `POST /bookings/change` · `POST /flights/delay` | 口岸预约、改约、航班延误 |
| `POST /temperatures` · `POST /temperatures/resolve` | 冷链温度上报（自动判异常）与处置 |
| `POST /shipments` · `GET /shipments/{id}` · `GET /shipments/{id}/release` · `POST /shipments/{id}/depart` · `POST /shipments/{id}/reject` | 出运计划、放行结论（补正/改约/延误/温控后自动失效重判，不沿用旧结论）、出运、拒收退运回溯 |
| `GET /settlements/{farmer_id}` | 农户结算 |
| `GET /rules` · `GET /events` | 目的地规则（按计划出运时间选生效版本）与事件时间线 |

评估类接口接受 `?now=` ISO 时间参数，事件类接口接受 `at` 字段，便于演示与对账。
