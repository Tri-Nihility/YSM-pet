"""YSM / Bedrock Molang 迷你求值器。

为什么需要它
------------
`.ysm` 里大量关键帧不是数字，而是 **Molang 表达式**，例如慧慧1.2版本的
`pre_parallel0`（常驻层，永远在播）：

    FZ   scale = [0,0,0]                          ← 作者真要隐藏
    DAZ  scale = ["!v.roaming.p", ...]             ← 漫游变体没开时要 **显示**
    ST   scale = ["!v.roaming.p", ...]

以前 `ysm.animation._vec()` 把「不是数字的字符串」一律当 **0** 处理，于是
`!v.roaming.p` → 0 → scale=0 → 身体/服装骨骼全被当成隐藏，
人物只剩一个 18×81 px 的竖条（实测占窗 3%，用户报的「窗口是竖长方形但没内容」）。

本模块只做「够渲染用」的子集：算术、比较、逻辑、三元、`math.*` 函数、
`v.` / `q.` / `query.` / `ysm.` 变量、以及 `v.x=...;` 赋值语句。
求值不了的表达式由调用方兜底（返回 None → 调用方按旧行为处理）。

变量表
------
`VARS` 是模块级字典（键一律小写）。渲染线程每帧调 `update_vars()` 写当前
的 `ysm.head_yaw` / `ysm.head_pitch` / `q.anim_time` 等；模型自己的
`v.xxx=...` 赋值也会写进同一张表（与原版 Molang 一致：`v.` 是每个模型
自己的变量空间）。
"""
import math
import re

__all__ = ["eval_expr", "update_vars", "set_vars", "get_vars", "MolangError"]

# 运行时变量表：{"v.roaming.p": 0.0, "ysm.head_yaw": 12.0, ...}
VARS = {}

# 解析结果缓存：表达式串 -> AST（语句列表）
_AST = {}
_AST_CAP = 20000

_NUM_RE = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<num>\d+\.\d*|\.\d+|\d+)
      | (?P<id>[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_0-9]+)*)
      | (?P<op><=|>=|==|!=|&&|\|\||[-+*/%()<>=!?:;,.])
    )""", re.X)


class MolangError(Exception):
    """表达式看不懂（调用方兜底，不要往外冒）。"""


def _is_number(text):
    """能不能当纯数字读（Molang 里 "1.3" 这种字符串常量很常见）。"""
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------- 变量
def update_vars(mapping):
    """把一批运行时变量写进表（值统一转 float；转不了的忽略）。"""
    for k, v in (mapping or {}).items():
        try:
            VARS[str(k).lower()] = float(v)
        except (TypeError, ValueError):
            continue


def set_vars(mapping):
    """清空后重设（换模型时用）。"""
    VARS.clear()
    update_vars(mapping)


def get_vars():
    return dict(VARS)


def _lookup(name):
    """变量取值：精确 → 别名（query./q.、ysm.）→ 未知一律 0。"""
    n = name.lower()
    v = VARS.get(n)
    if v is not None:
        return float(v)
    if n.startswith("query."):
        v = VARS.get("q." + n[6:])
    elif n.startswith("q."):
        v = VARS.get("query." + n[2:])
    if v is not None:
        return float(v)
    # 常见全局量：没有就 0（与原版未赋值变量行为一致）
    if n in ("pi", "math.pi"):
        return math.pi
    if n == "e":
        return math.e
    return 0.0


# --------------------------------------------------------------------- 词法
def _tokenize(src):
    out = []
    i, n = 0, len(src)
    while i < n:
        m = _TOKEN_RE.match(src, i)
        if not m:
            if src[i].isspace():
                i += 1
                continue
            raise MolangError("无法识别的位置 %d: %r" % (i, src[i:i + 12]))
        i = m.end()
        if m.group("num") is not None:
            out.append(("num", float(m.group("num"))))
        elif m.group("id") is not None:
            out.append(("id", m.group("id")))
        else:
            out.append(("op", m.group("op")))
    return out


# --------------------------------------------------------------------- 语法
# AST 节点：("num", v) ("var", name) ("un", op, a) ("bin", op, a, b)
#          ("tern", c, a, b) ("call", name, [args]) ("assign", name, expr)
def _parse(tokens):
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else (None, None)

    def take():
        tok = peek()
        pos[0] += 1
        return tok

    def expect_op(op):
        kind, val = take()
        if kind != "op" or val != op:
            raise MolangError("期望 %r，实际 %r" % (op, val))

    def at_op(op):
        kind, val = peek()
        return kind == "op" and val == op

    def program():
        stmts = []
        while pos[0] < len(tokens):
            if at_op(";"):
                take()
                continue
            stmts.append(statement())
            if at_op(";"):
                take()
        return stmts

    def statement():
        # 赋值： ID = expr   （Molang 里 v.x=expr 是合法语句，返回赋值结果）
        if pos[0] + 1 < len(tokens):
            k0, v0 = tokens[pos[0]]
            k1, v1 = tokens[pos[0] + 1]
            if k0 == "id" and k1 == "op" and v1 == "=":
                take()
                take()
                return ("assign", v0, expr())
        return expr()

    def expr():
        return ternary()

    def ternary():
        cond = or_()
        if at_op("?"):
            take()
            a = expr()
            # Molang 允许省略 else：`cond ? a`（等价于 `cond ? a : 0`）。
            # 实测慧慧1.2 里 `(v.qh==1?(0)) + (v.qh==2?(0)) + (v.jump?(0));`
            # 就是这种写法，出现 54 次；不认它会让整条表达式解析失败 → 退回 0。
            if at_op(":"):
                take()
                b = expr()
            else:
                b = ("num", 0.0)
            return ("tern", cond, a, b)
        return cond

    def or_():
        node = and_()
        while at_op("||"):
            take()
            node = ("bin", "||", node, and_())
        return node

    def and_():
        node = cmp_()
        while at_op("&&"):
            take()
            node = ("bin", "&&", node, cmp_())
        return node

    def cmp_():
        node = add()
        while True:
            kind, val = peek()
            if kind == "op" and val in ("<", ">", "<=", ">=", "==", "!="):
                take()
                node = ("bin", val, node, add())
            else:
                return node

    def add():
        node = mul()
        while True:
            kind, val = peek()
            if kind == "op" and val in ("+", "-"):
                take()
                node = ("bin", val, node, mul())
            else:
                return node

    def mul():
        node = unary()
        while True:
            kind, val = peek()
            if kind == "op" and val in ("*", "/", "%"):
                take()
                node = ("bin", val, node, unary())
            else:
                return node

    def unary():
        kind, val = peek()
        if kind == "op" and val in ("-", "!"):
            take()
            return ("un", val, unary())
        if kind == "op" and val == "+":
            take()
            return unary()
        return primary()

    def primary():
        kind, val = take()
        if kind == "num":
            return ("num", val)
        if kind == "op" and val == "(":
            node = expr()
            expect_op(")")
            return node
        if kind == "id":
            nxt_kind, nxt_val = peek()
            if nxt_kind == "op" and nxt_val == "(":
                take()
                args = []
                if not at_op(")"):
                    args.append(expr())
                    while at_op(","):
                        take()
                        args.append(expr())
                expect_op(")")
                return ("call", val, args)
            return ("var", val)
        raise MolangError("意外的记号 %r" % (val,))

    return program()


def _compile(text):
    ast = _AST.get(text)
    if ast is None:
        ast = _parse(_tokenize(text))
        if len(_AST) >= _AST_CAP:
            _AST.clear()
        _AST[text] = ast
    return ast


# --------------------------------------------------------------------- 求值
_FUNCS = {
    "math.abs": abs, "math.floor": math.floor, "math.ceil": math.ceil,
    "math.round": lambda x: float(round(x)), "math.sqrt": math.sqrt,
    "math.sin": math.sin, "math.cos": math.cos, "math.tan": math.tan,
    "math.asin": math.asin, "math.acos": math.acos, "math.atan": math.atan,
    "math.exp": math.exp, "math.pow": lambda a, b: a ** b,
    "math.min": min, "math.max": max, "math.mod": math.fmod,
    "math.trunc": math.trunc,
    "math.lerp": lambda a, b, t: a + (b - a) * t,
    "math.clamp": lambda x, lo, hi: max(lo, min(hi, x)),
    "math.hermite_blend": lambda t: t * t * (3 - 2 * t),
    "math.random": lambda a=0.0, b=1.0: a,       # 桌宠不需要随机抖动，取端值
    "math.random_integer": lambda a=0.0, b=1.0: a,
    "math.die_roll": lambda *a: 0.0,
}


def _truth(v):
    return 1.0 if v else 0.0


def _ev(node):
    tag = node[0]
    if tag == "num":
        return float(node[1])
    if tag == "var":
        return _lookup(node[1])
    if tag == "assign":
        val = _ev(node[2])
        VARS[node[1].lower()] = val
        return val
    if tag == "un":
        if node[1] == "-":
            return -_ev(node[2])
        return 0.0 if _ev(node[2]) else 1.0           # !
    if tag == "bin":
        op = node[1]
        if op == "&&":
            return _truth(_ev(node[2]) and _ev(node[3]))
        if op == "||":
            return _truth(_ev(node[2]) or _ev(node[3]))
        a, b = _ev(node[2]), _ev(node[3])
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b if b else 0.0
        if op == "%":
            return math.fmod(a, b) if b else 0.0
        if op == "<":
            return _truth(a < b)
        if op == ">":
            return _truth(a > b)
        if op == "<=":
            return _truth(a <= b)
        if op == ">=":
            return _truth(a >= b)
        if op == "==":
            return _truth(abs(a - b) < 1e-6)
        if op == "!=":
            return _truth(abs(a - b) >= 1e-6)
        raise MolangError("未知运算符 %r" % (op,))
    if tag == "tern":
        return _ev(node[2]) if _ev(node[1]) else _ev(node[3])
    if tag == "call":
        name = node[1].lower()
        args = [_ev(a) for a in node[2]]
        fn = _FUNCS.get(name)
        if fn is None:
            if name.startswith("math."):
                # 认不出的 math.* 函数：按 0 算，别整条表达式作废
                return 0.0
            return 0.0
        try:
            return float(fn(*args))
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    raise MolangError("未知节点 %r" % (tag,))


def eval_expr(text):
    """求值一个 Molang 表达式/语句串 -> float；看不懂抛 MolangError。"""
    stmts = _compile(text)
    if not stmts:
        return 0.0
    val = 0.0
    for s in stmts:
        val = _ev(s)
    return float(val)


def try_eval(text, default=None):
    """求值，失败返回 default（不想处理异常时用这个）。"""
    try:
        return eval_expr(text)
    except (MolangError, RecursionError, ZeroDivisionError,
            TypeError, ValueError):
        return default
