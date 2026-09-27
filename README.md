# 法律证据保管与流转后台

仅使用 Python 3.11+ 标准库实现的证据保管项目。支持真实 SHA-256 入册、封存/开箱、两步移交（发起/确认/撤回/重新提交）、分析衍生关系、案件成员权限、法律保留、保留期限、不可变保管事件链和 JSON 报告导出。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8105>，默认数据库 `custody.db`。测试：

```bash
python3 -m unittest -v
```

演示身份：`custodian1`、`custodian2`、`analyst1`、`auditor1`、`outsider`。请求使用 `X-User-Id`。

## 主要接口

- `POST /api/cases`：创建案件，创建人自动成为保管员。
- `POST /api/cases/{id}/members`：授予 custodian、analyst 或 auditor 角色。
- `POST /api/cases/{id}/evidence`：以 Base64 入册证据，服务端计算 SHA-256 和大小。
- `GET /api/evidence/{id}`：查看元数据、完整保管事件链、完整性结果和衍生关系。
- `POST /api/evidence/{id}/open`：保管员开箱。
- `POST /api/evidence/{id}/transfer`：保管员发起两步移交，生成交接编号；证据进入交接中，开箱、派生、释放和再次移交暂停，接收人须为本案有效成员。
- `POST /api/evidence/{id}/transfer/confirm`：待接收人确认后才更新保管人；若交接期间出现法律保留，返回保留原因并保留待处理交接。
- `POST /api/evidence/{id}/transfer/withdraw`：原发起人撤回待处理交接，证据恢复正常操作。
- `POST /api/evidence/{id}/transfer/resubmit`：法律保留解除后，由原发起人重新提交交接，接收人再次确认。
- `POST /api/evidence/{id}/derive`：分析员从已开箱证据创建衍生证据。
- `POST /api/evidence/{id}/hold`：审计员或案件创建人设置/解除法律保留。
- `POST /api/evidence/{id}/release`：存在法律保留时拒绝释放。
- `GET /api/cases/{id}/report`：校验所有证据哈希和每条事件链，导出完整报告。
- 所有 `DELETE` 请求返回 405；证据和保管记录不提供删除接口。

保管事件通过前一条事件哈希串联；报告会重新计算文件哈希和事件链。项目适合流程与完整性原型，不涵盖现实中的签名证书、WORM 存储、证据文件加密或司法辖区合规认证。
