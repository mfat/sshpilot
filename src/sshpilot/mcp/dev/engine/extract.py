"""Python AST extraction: definitions and provenance-labelled edges.

One file in, plain records out. Nothing is imported or executed. Each edge
carries the *mechanism* that produced it and an extraction-time confidence:

* ``static``     – the target name is bound by an import or a module-level
  definition visible at the use site (resolution to an indexed symbol happens
  at query time; an unresolvable static target is reported as unresolved);
* ``heuristic``  – ``self.m()`` / ``super().m()`` dispatch, callbacks passed
  by reference, GTK ``connect("signal", handler)`` wiring;
* ``unresolved`` – attribute calls on objects whose type is unknown,
  ``getattr`` with a computed name, star-import names.

This deliberately is not a whole-program call graph: no type inference, no
data-flow, no runtime dispatch. Callers see the label, never a silent guess.
"""

from __future__ import annotations

import ast
import builtins
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: Extraction-time confidence labels (see module docstring).
STATIC = "static"
HEURISTIC = "heuristic"
UNRESOLVED = "unresolved"

#: Placeholder prefix for an attribute target whose owner is unknown.
UNKNOWN_OWNER = "?."
#: Qualname used for the module symbol itself.
MODULE_QUALNAME = "<module>"

_BUILTINS = frozenset(dir(builtins))
_MAX_SIGNATURE = 200
_MAX_DOC = 120


@dataclass
class SymbolRecord:
    qualname: str  # unique within the file (``@2`` suffix for duplicates)
    name: str
    fqname: str  # dotted module path + qualname (no duplicate suffix)
    kind: str
    line: int
    end_line: int
    signature: str = ""
    doc: str = ""
    parent: Optional[str] = None


@dataclass
class EdgeRecord:
    src: str  # qualname of the enclosing symbol
    kind: str  # imports | calls | references | inherits | connects_signal
    target: str  # dotted fq name, or "?.<attr>" when the owner is unknown
    line: int
    confidence: str
    mechanism: str
    detail: str = ""


@dataclass
class FileExtract:
    module: str
    status: str  # ok | syntax_error
    error: str = ""
    symbols: List[SymbolRecord] = field(default_factory=list)
    edges: List[EdgeRecord] = field(default_factory=list)


def module_name_for(path: str) -> str:
    """Map a repository-relative ``.py`` path to its dotted module name."""
    parts = path[:-3].split("/") if path.endswith(".py") else path.split("/")
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(part for part in parts if part)


def _is_package(path: str) -> bool:
    return path.endswith("/__init__.py") or path == "__init__.py"


def _resolve_relative(module: str, is_package: bool, level: int, name: Optional[str]) -> str:
    parts = module.split(".") if module else []
    package = parts if is_package else parts[:-1]
    if level > 1:
        package = package[: max(0, len(package) - (level - 1))]
    base = ".".join(package)
    if name:
        return f"{base}.{name}" if base else name
    return base


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _signature(node) -> str:
    args = node.args
    pieces: List[str] = []

    def render(arg: ast.arg, default: bool) -> str:
        text = arg.arg
        if arg.annotation is not None:
            try:
                text += ": " + ast.unparse(arg.annotation)
            except Exception:  # pragma: no cover - defensive
                text += ": ?"
        # Defaults are omitted on purpose: literals in defaults may be
        # sensitive and are not needed to navigate code.
        return text + ("=…" if default else "")

    positional = [*args.posonlyargs, *args.args]
    first_default = len(positional) - len(args.defaults)
    for index, arg in enumerate(positional):
        pieces.append(render(arg, index >= first_default))
    if args.vararg:
        pieces.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        pieces.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        pieces.append(render(arg, default is not None))
    if args.kwarg:
        pieces.append("**" + args.kwarg.arg)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    text = f"{prefix} {node.name}({', '.join(pieces)})"
    if node.returns is not None:
        try:
            text += " -> " + ast.unparse(node.returns)
        except Exception:  # pragma: no cover - defensive
            pass
    return _clip(text, _MAX_SIGNATURE)


def _doc(node) -> str:
    try:
        doc = ast.get_docstring(node, clean=True)
    except Exception:  # pragma: no cover - defensive
        return ""
    if not doc:
        return ""
    return _clip(doc.strip().splitlines()[0], _MAX_DOC)


def _chain(node: ast.AST) -> Optional[List[str]]:
    """Flatten ``a.b.c`` into ``["a", "b", "c"]`` (None if not a Name chain)."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


class _Scope:
    """A function or class scope during the walk."""

    def __init__(self, qualname: str, kind: str, fqname: str, class_fq: Optional[str]):
        self.qualname = qualname
        self.kind = kind
        self.fqname = fqname
        # Owning class of a method (for self./super() resolution).
        self.class_fq = class_fq
        # Function-local name bindings: name -> fq (local def/import) or None.
        self.locals: Dict[str, Optional[str]] = {}
        # For classes: names of methods defined directly in the body.
        self.members: frozenset = frozenset()
        # For methods: the owning class scope.
        self.owner: Optional["_Scope"] = None


class _Extractor:
    def __init__(self, path: str, module: str):
        self.path = path
        self.module = module
        self.is_package = _is_package(path)
        self.symbols: List[SymbolRecord] = []
        self.edges: List[EdgeRecord] = []
        self.aliases: Dict[str, str] = {}  # module-level imports
        self.module_defs: Dict[str, str] = {}  # module-level def/class/assign
        self.module_callables: set = set()  # module-level def/class names
        self.star_imports: List[str] = []
        self._qualcount: Dict[str, int] = {}
        self._call_funcs: set = set()

    # -- symbols -------------------------------------------------------------
    def _add_symbol(self, qual: str, name: str, kind: str, node, parent, signature="", doc="") -> str:
        count = self._qualcount.get(qual, 0) + 1
        self._qualcount[qual] = count
        unique = qual if count == 1 else f"{qual}@{count}"
        start = min(
            [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
        )
        self.symbols.append(
            SymbolRecord(
                qualname=unique,
                name=name,
                fqname=f"{self.module}.{qual}" if self.module else qual,
                kind=kind,
                line=start,
                end_line=getattr(node, "end_lineno", None) or node.lineno,
                signature=signature,
                doc=doc,
                parent=parent,
            )
        )
        return unique

    def _edge(self, src: str, kind: str, target: str, line: int, confidence: str, mechanism: str, detail: str = "") -> None:
        self.edges.append(EdgeRecord(src, kind, target, line, confidence, mechanism, detail))

    # -- pre-pass: module-level bindings ------------------------------------
    def _collect_module_bindings(self, tree: ast.Module) -> None:
        for node in tree.body:
            for sub in self._flatten_conditional(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.module_defs[sub.name] = f"{self.module}.{sub.name}"
                    self.module_callables.add(sub.name)
                elif isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            self.module_defs[target.id] = f"{self.module}.{target.id}"
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for local, fq in self._import_bindings(sub):
                        if local == "*":
                            self.star_imports.append(fq)
                        else:
                            self.aliases[local] = fq

    @staticmethod
    def _flatten_conditional(node: ast.stmt):
        """Yield *node* plus statements nested in module-level if/try blocks."""
        stack = [node]
        while stack:
            current = stack.pop(0)
            yield current
            if isinstance(current, ast.If):
                stack.extend(current.body + current.orelse)
            elif isinstance(current, ast.Try):
                stack.extend(current.body + current.orelse + current.finalbody)
                for handler in current.handlers:
                    stack.extend(handler.body)

    def _import_bindings(self, node) -> List[Tuple[str, str]]:
        bindings = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings.append((alias.asname, alias.name))
                else:
                    # ``import a.b`` binds ``a``.
                    head = alias.name.split(".")[0]
                    bindings.append((head, head))
        else:
            if node.level:
                base = _resolve_relative(self.module, self.is_package, node.level, node.module)
            else:
                base = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    bindings.append(("*", base))
                    continue
                fq = f"{base}.{alias.name}" if base else alias.name
                bindings.append((alias.asname or alias.name, fq))
        return bindings

    # -- resolution ----------------------------------------------------------
    def _resolve_name(self, name: str, scopes: List[_Scope]) -> Tuple[Optional[str], str]:
        """Return (fq or None, mechanism)."""
        for scope in reversed(scopes):
            if scope.kind == "class":
                continue  # class bodies are not visible from methods
            if name in scope.locals:
                fq = scope.locals[name]
                if fq and fq != "<param>":
                    return fq, "local-binding"
                return None, "local-variable"
        if name in self.aliases:
            return self.aliases[name], "import-alias"
        if name in self.module_defs:
            return self.module_defs[name], "module-definition"
        if name in _BUILTINS:
            return None, "builtin"
        if self.star_imports:
            return f"{self.star_imports[0]}.{name}", "star-import"
        return None, "unknown-name"

    def _resolve_callee(self, func: ast.AST, scopes: List[_Scope]) -> Optional[Tuple[str, str, str, str]]:
        """Return (target, confidence, mechanism, detail) for a call target."""
        method_scope = self._method_scope(scopes)
        # super().m(...)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Call)
            and isinstance(func.value.func, ast.Name)
            and func.value.func.id == "super"
            and method_scope is not None
        ):
            return f"{method_scope.class_fq}.{func.attr}", HEURISTIC, "super-call", "super"
        # getattr(obj, "name")(...)
        if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr":
            if len(func.args) >= 2 and isinstance(func.args[1], ast.Constant) and isinstance(func.args[1].value, str):
                return UNKNOWN_OWNER + func.args[1].value, UNRESOLVED, "getattr-literal", ""
            return UNKNOWN_OWNER + "<dynamic>", UNRESOLVED, "getattr-dynamic", ""
        chain = _chain(func)
        if chain is None:
            if isinstance(func, ast.Attribute):
                return UNKNOWN_OWNER + func.attr, UNRESOLVED, "attribute-call", ""
            return None
        head, rest = chain[0], chain[1:]
        if head in ("self", "cls") and method_scope is not None and not self._is_local(head, scopes, method_scope):
            if not rest:
                return None  # ``self(...)``: __call__ on the instance
            if len(rest) == 1:
                return f"{method_scope.class_fq}.{rest[0]}", HEURISTIC, "self-method", ""
            return UNKNOWN_OWNER + rest[-1], UNRESOLVED, "self-attribute-chain", ".".join(chain)
        fq, mechanism = self._resolve_name(head, scopes)
        if fq is None:
            if mechanism in ("builtin",) and not rest:
                return None  # builtins are noise for this index
            if rest:
                return UNKNOWN_OWNER + rest[-1], UNRESOLVED, "attribute-call", ""
            return UNKNOWN_OWNER + head, UNRESOLVED, mechanism, ""
        target = ".".join([fq, *rest])
        confidence = UNRESOLVED if mechanism == "star-import" else STATIC
        return target, confidence, mechanism, ""

    @staticmethod
    def _method_scope(scopes: List[_Scope]) -> Optional[_Scope]:
        for scope in reversed(scopes):
            if scope.kind in ("function", "method") and scope.class_fq:
                return scope
            if scope.kind == "class":
                return None
        return None

    @staticmethod
    def _is_local(name: str, scopes: List[_Scope], method_scope: _Scope) -> bool:
        # ``self`` rebound inside the method body is not the instance.
        return method_scope.locals.get(name, "<param>") not in (None, "<param>")

    # -- walk ----------------------------------------------------------------
    def run(self, tree: ast.Module) -> None:
        self._collect_module_bindings(tree)
        module_end = max((getattr(n, "end_lineno", 1) or 1 for n in tree.body), default=1)
        self.symbols.append(
            SymbolRecord(
                qualname=MODULE_QUALNAME,
                name=self.module.rsplit(".", 1)[-1] if self.module else self.path,
                fqname=self.module,
                kind="module",
                line=1,
                end_line=module_end,
                doc=_doc(tree),
            )
        )
        self._body(tree.body, [], MODULE_QUALNAME, "")

    def _body(self, statements, scopes: List[_Scope], owner: str, prefix: str) -> None:
        for statement in statements:
            self._statement(statement, scopes, owner, prefix)

    def _statement(self, node: ast.stmt, scopes: List[_Scope], owner: str, prefix: str) -> None:
        in_class = bool(scopes) and scopes[-1].kind == "class"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = f"{prefix}{node.name}"
            if in_class:
                kind = "async_method" if isinstance(node, ast.AsyncFunctionDef) else "method"
            else:
                kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            unique = self._add_symbol(qual, node.name, kind, node, owner, _signature(node), _doc(node))
            fq = f"{self.module}.{qual}"
            if scopes and scopes[-1].kind != "class":
                scopes[-1].locals[node.name] = fq
            for decorator in node.decorator_list:
                self._expressions(decorator, scopes, owner)
            for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]:
                self._expressions(default, scopes, owner)
            class_fq = scopes[-1].fqname if in_class else None
            scope = _Scope(unique, "method" if in_class else "function", fq, class_fq)
            scope.owner = scopes[-1] if in_class else None
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                scope.locals[arg.arg] = None
            if node.args.vararg:
                scope.locals[node.args.vararg.arg] = None
            if node.args.kwarg:
                scope.locals[node.args.kwarg.arg] = None
            if in_class and (node.args.posonlyargs or node.args.args):
                first = (node.args.posonlyargs or node.args.args)[0].arg
                scope.locals[first] = "<param>"  # self/cls marker
            self._prebind(node.body, scope)
            self._body(node.body, [*scopes, scope], unique, f"{qual}.")
            return
        if isinstance(node, ast.ClassDef):
            qual = f"{prefix}{node.name}"
            unique = self._add_symbol(qual, node.name, "class", node, owner, f"class {node.name}", _doc(node))
            fq = f"{self.module}.{qual}"
            if scopes and scopes[-1].kind != "class":
                scopes[-1].locals[node.name] = fq
            for base in node.bases:
                chain = _chain(base)
                if chain is None:
                    continue
                target, mechanism = self._resolve_name(chain[0], scopes)
                if target is None:
                    self._edge(unique, "inherits", UNKNOWN_OWNER + chain[-1], node.lineno, UNRESOLVED, mechanism)
                else:
                    confidence = UNRESOLVED if mechanism == "star-import" else STATIC
                    self._edge(unique, "inherits", ".".join([target, *chain[1:]]), node.lineno, confidence, mechanism)
            for decorator in node.decorator_list:
                self._expressions(decorator, scopes, owner)
            scope = _Scope(unique, "class", fq, None)
            scope.members = frozenset(
                item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            self._body(node.body, [*scopes, scope], unique, f"{qual}.")
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for local, fq in self._import_bindings(node):
                mechanism = "import" if isinstance(node, ast.Import) else "import-from"
                self._edge(owner, "imports", fq, node.lineno, STATIC, mechanism, local)
                if scopes and scopes[-1].kind != "class" and local != "*":
                    scopes[-1].locals[local] = fq
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and (not scopes or scopes[-1].kind == "class"):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    qual = f"{prefix}{target.id}"
                    kind = "attribute" if scopes else "variable"
                    self._add_symbol(qual, target.id, kind, node, owner)
        if isinstance(node, (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)) or (
            hasattr(ast, "TryStar") and isinstance(node, getattr(ast, "TryStar"))
        ):
            # Walk headers as expressions, nested statements as statements so
            # conditional definitions are still indexed.
            for name, value in ast.iter_fields(node):
                if name in ("body", "orelse", "finalbody"):
                    self._body(value, scopes, owner, prefix)
                elif name == "handlers":
                    for handler in value:
                        if handler.type is not None:
                            self._expressions(handler.type, scopes, owner)
                        self._body(handler.body, scopes, owner, prefix)
                elif isinstance(value, ast.AST):
                    self._expressions(value, scopes, owner)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, ast.AST):
                            self._expressions(item, scopes, owner)
            return
        if isinstance(node, ast.Match if hasattr(ast, "Match") else ()):
            self._expressions(node.subject, scopes, owner)
            for case in node.cases:
                self._body(case.body, scopes, owner, prefix)
            return
        self._expressions(node, scopes, owner)

    @staticmethod
    def _prebind(statements, scope: _Scope) -> None:
        """Record function-local assignments so they shadow module names."""
        for node in statements:
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and sub is not node:
                    # Nested scopes bind their own names; the def name itself
                    # is bound when the statement is visited.
                    pass
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    scope.locals.setdefault(sub.id, None)
                elif isinstance(sub, ast.arg):
                    pass

    def _expressions(self, node: ast.AST, scopes: List[_Scope], owner: str) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                self._call(sub, scopes, owner)
            elif isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load):
                if id(sub) in self._call_funcs:
                    continue
                if isinstance(sub.value, ast.Name) and sub.value.id in ("self", "cls"):
                    # ``self._handle_x`` stored in a table or passed around.
                    reference = self._callable_reference(sub, scopes)
                    if reference is not None:
                        self._edge(owner, "references", reference[0], sub.lineno, HEURISTIC, "self-method-load")
                    continue
                self._reference(sub, scopes, owner)

    def _call(self, call: ast.Call, scopes: List[_Scope], owner: str) -> None:
        # The callee chain (``a.b.c`` in ``a.b.c()``) is described by the call
        # edge; do not record its prefixes again as references.
        node = call.func
        while isinstance(node, ast.Attribute):
            self._call_funcs.add(id(node))
            node = node.value
        resolved = self._resolve_callee(call.func, scopes)
        if resolved is not None:
            target, confidence, mechanism, detail = resolved
            self._edge(owner, "calls", target, call.lineno, confidence, mechanism, detail)
        # GTK/GObject signal wiring: obj.connect("signal", handler, ...)
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in ("connect", "connect_after")
            and len(call.args) >= 2
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            handler = self._callable_reference(call.args[1], scopes)
            if handler is not None:
                self._edge(owner, "connects_signal", handler[0], call.lineno, HEURISTIC, handler[1], call.args[0].value)
                self._call_funcs.add(id(call.args[1]))
        # Callbacks passed by reference: GLib.idle_add(self._tick), Thread(target=f)
        for argument in [*call.args, *[kw.value for kw in call.keywords]]:
            if id(argument) in self._call_funcs:
                continue  # already recorded as a signal handler
            reference = self._callable_reference(argument, scopes)
            if reference is not None:
                self._call_funcs.add(id(argument))
                self._edge(owner, "references", reference[0], call.lineno, HEURISTIC, "callback-argument")

    def _callable_reference(self, node: ast.AST, scopes: List[_Scope]) -> Optional[Tuple[str, str]]:
        chain = _chain(node)
        if chain is None:
            return None
        method_scope = self._method_scope(scopes)
        if chain[0] in ("self", "cls") and len(chain) == 2 and method_scope is not None:
            # Only methods the class itself defines: data attributes passed as
            # arguments are not callbacks, and inherited ones are not knowable
            # here (query-time base lookup would be a guess on a guess).
            if method_scope.owner is not None and chain[1] in method_scope.owner.members:
                return f"{method_scope.class_fq}.{chain[1]}", "self-method-reference"
            return None
        if len(chain) == 1:
            fq, mechanism = self._resolve_name(chain[0], scopes)
            if fq is None:
                return None
            if mechanism == "module-definition" and chain[0] not in self.module_callables:
                return None
            if mechanism in ("module-definition", "import-alias", "local-binding"):
                return fq, mechanism
        return None

    def _reference(self, node: ast.Attribute, scopes: List[_Scope], owner: str) -> None:
        """Record ``Alias.attr`` loads rooted in an import or module definition
        (``EventType.X``, ``Capability.Y``, ``module.CONSTANT``)."""
        chain = _chain(node)
        if chain is None or chain[0] in ("self", "cls"):
            return
        fq, mechanism = self._resolve_name(chain[0], scopes)
        if fq is None or mechanism not in ("import-alias", "module-definition"):
            return
        if not fq.startswith(("sshpilot", "tests", "scripts")) and not fq.startswith(self.module):
            return  # external library attribute: noise for this index
        self._edge(owner, "references", ".".join([fq, *chain[1:]]), node.lineno, STATIC, mechanism)


def extract_source(path: str, source: str) -> FileExtract:
    """Extract symbols and edges from one Python source file."""
    module = module_name_for(path)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as error:
        line = getattr(error, "lineno", None)
        where = f" (line {line})" if line else ""
        return FileExtract(module=module, status="syntax_error", error=f"{type(error).__name__}{where}: {getattr(error, 'msg', error)}")
    extractor = _Extractor(path, module)
    extractor.run(tree)
    # Attribute nodes inside a reference chain also appear as standalone
    # Attribute nodes during ast.walk; keep only the longest chain per line.
    edges = _dedupe_references(extractor.edges)
    return FileExtract(module=module, status="ok", symbols=extractor.symbols, edges=edges)


def _dedupe_references(edges: List[EdgeRecord]) -> List[EdgeRecord]:
    references: Dict[Tuple[str, int], List[EdgeRecord]] = {}
    others: List[EdgeRecord] = []
    for edge in edges:
        if edge.kind == "references" and edge.mechanism in ("import-alias", "module-definition"):
            references.setdefault((edge.src, edge.line), []).append(edge)
        else:
            others.append(edge)
    kept: List[EdgeRecord] = []
    for group in references.values():
        targets = {edge.target for edge in group}
        for edge in group:
            # Drop ``a.b`` when ``a.b.c`` was recorded at the same site.
            if any(other != edge.target and other.startswith(edge.target + ".") for other in targets):
                continue
            if edge in kept:
                continue
            kept.append(edge)
    seen = set()
    result = []
    for edge in [*others, *kept]:
        key = (edge.src, edge.kind, edge.target, edge.line, edge.mechanism, edge.detail)
        if key in seen:
            continue
        seen.add(key)
        result.append(edge)
    result.sort(key=lambda e: (e.line, e.kind, e.target))
    return result
