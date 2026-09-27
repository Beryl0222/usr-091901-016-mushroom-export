"""角色与数据可见性：企业资料隔离、查验按职责读取、结算不暴露买方合同。"""
from dataclasses import dataclass

ROLES = ("enterprise", "inspector_local", "inspector_port", "settlement", "dispatcher", "carrier")
OPS_ROLES = ("inspector_local", "inspector_port", "dispatcher")


@dataclass
class Principal:
    """调用者身份：角色加可选企业编号。"""
    role: str
    enterprise_id: str = ""

    @property
    def is_ops(self):
        return self.role in OPS_ROLES


def parse_principal(header):
    """解析 X-Actor 头，形如 enterprise:ENT1 或 dispatcher。"""
    if not header:
        return None
    role, _, org = header.partition(":")
    role = role.strip()
    if role not in ROLES:
        return None
    if role == "enterprise" and not org.strip():
        return None
    return Principal(role, org.strip())


def can_read_enterprise(p, enterprise_id):
    """企业仅见自己的资料；查验与调度按职责读取；结算与承运不读业务单据。"""
    if p.role == "enterprise":
        return p.enterprise_id == enterprise_id
    return p.is_ops


def shipment_view(data, p, owner_enterprise_id):
    """买方合同仅货主企业可见，其他角色一律脱敏。"""
    if p.role == "enterprise" and p.enterprise_id == owner_enterprise_id:
        return data
    data = dict(data)
    data.pop("buyer_contract", None)
    return data
