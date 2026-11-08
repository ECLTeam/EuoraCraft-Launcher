import hashlib
import random
import uuid


def generate_code() -> str:
    r"""
    :return: Scaffolding协议标准联机房间码
    """
    charset: str = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    # 生成能被7整除的随机数
    max_value = 34 ** 16
    value = random.randrange(0, max_value, 7)
    # 转换为34进制（小端序）
    code_chars = []
    temp_value = value
    for i in range(16):
        code_chars.append(charset[temp_value % 34])
        temp_value //= 34
    code_str = "".join(code_chars)
    # 格式化为 U/NNNN-NNNN-SSSS-SSSS
    formatted_code = f"U/{code_str[0:4]}-{code_str[4:8]}-{code_str[8:12]}-{code_str[12:16]}"
    return formatted_code


def validate_code(code: str) -> bool:
    r"""
    验证联机房间码是否为Scaffolding协议标准联机房间码
    :param code: 联机房间码
    :return: bool
    """
    charset = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    # 解析代码
    if not code.startswith("U/"):
        return False
    parts = code[2:].split("-")
    if len(parts) != 4 or any(len(part) != 4 for part in parts):
        return False
    # 合并所有字符（小端序序列）
    all_chars = "".join(parts)
    # 检查字符是否合法
    for char in all_chars:
        if char not in charset:
            return False
    # 按小端序计算数值（第一个字符对应最低位）
    total = 0
    for i, char in enumerate(all_chars):
        char_value = charset.index(char)
        total += char_value * (34 ** i)
    return total % 7 == 0


def machine_id() -> str:
    r"""
    这里使用MAC地址进行MD5加密作为machine_id, MAC地址虽然可以修改, 但是非常难遇到两个相同MAC地址的人一起联机
    :return: machine_id
    """
    mac_int = uuid.getnode()
    try:
        mac_bytes = mac_int.to_bytes(6, byteorder="big")
    except OverflowError:
        # 如果MAC地址值异常，使用位掩码确保在48位范围内
        mac_int = mac_int & 0xFFFFFFFFFFFF  # 48位掩码
        mac_bytes = mac_int.to_bytes(6, byteorder="big")
    return hashlib.md5(mac_bytes).hexdigest()

