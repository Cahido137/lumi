"""计算器工具"""
import ast
import operator

from langchain_core.tools import tool


# AST 节点类型
_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,   # 负号 -x
    ast.UAdd: operator.pos,   # 正号 +x
}


def _safe_eval(node: ast.AST):
    """只执行白名单内的纯数学运算"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("表达式含不允许的运算")


@tool(parse_docstring=True)
def calculator(expression: str) -> str:
    """
    计算纯数学表达式，支持 + - * / % ** 和括号，例如 "2**10" 或 "(3+4)*5"。

    Args:
        expression: 数学表达式字符串

    Returns:
        计算结果
    """
    # M1 大改: 失败通过异常传播, 由执行层统一转为 status="error" 的工具消息,
    # 不再返回错误字符串
    try:
        result = _safe_eval(ast.parse(expression, mode="eval"))
        return str(result)
    except Exception as e:
        raise ValueError(f"计算失败: {e}") from e