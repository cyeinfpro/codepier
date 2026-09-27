"""Load production JavaScript for isolated behavior tests by syntax, not formatting.

Full-stack browser tests still exercise the real script tags and automatic boot.
Component adapters omit only the final top-level boot call, so no private URL or
real account is contacted while the harness configures controlled HTTP replies.
"""
from pathlib import Path
from tree_sitter import Language, Parser
import tree_sitter_javascript

ROOT = Path(__file__).resolve().parents[1]


def _panel_tree():
    raw = (ROOT / 'web/app.js').read_bytes()
    tree = Parser(Language(tree_sitter_javascript.language())).parse(raw)
    if tree.root_node.has_error:
        raise ValueError('Panel script has syntax errors')
    return raw, tree.root_node


def panel_without_boot():
    raw, root = _panel_tree()
    tail = root.named_children[-1]
    if tail.type != 'expression_statement' or len(tail.named_children) != 1:
        raise ValueError('Panel boot is no longer a final top-level expression')
    call = tail.named_children[0]
    function = call.child_by_field_name('function')
    if (call.type != 'call_expression' or function is None or function.type != 'parenthesized_expression'
            or len(function.named_children) != 1 or function.named_children[0].type != 'arrow_function'):
        raise ValueError('Panel boot is no longer a single arrow-function invocation')
    return raw[:tail.start_byte].decode('utf-8')


def panel_function(name):
    raw, root = _panel_tree()
    functions = [node for node in root.named_children if node.type == 'function_declaration'
                 and node.child_by_field_name('name').text.decode('utf-8') == name]
    if len(functions) != 1:
        raise ValueError('Expected one top-level panel function: ' + name)
    node = functions[0]
    return raw[node.start_byte:node.end_byte].decode('utf-8')
