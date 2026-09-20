"""Syntax-aware navigation. Reference candidates are not LSP type resolution."""
from __future__ import annotations
import ast
import json
from pathlib import Path
from shared.crypto import digest
from shared.util import DevError

SUPPORTED={'.py':'python','.js':'javascript','.mjs':'javascript','.cjs':'javascript','.jsx':'javascript',
           '.ts':'typescript','.mts':'typescript','.cts':'typescript','.tsx':'tsx'}
MAX_NODES=100000
MAX_METADATA_BYTES=2*1024*1024


def analyze(path,source):
    used=0
    def append(collection,item):
        nonlocal used
        used+=len(json.dumps(item,ensure_ascii=False).encode('utf-8'))+1
        if used>MAX_METADATA_BYTES:
            raise DevError('CODE_ANALYSIS_LIMIT','代码结构超过 2 MiB 元数据预算，请使用文本检索或缩小文件')
        collection.append(item)
    language=SUPPORTED.get(Path(path).suffix.lower())
    if language is None:
        raise DevError('UNSUPPORTED_LANGUAGE','结构检索目前支持 Python、JavaScript、TypeScript 和 TSX；其他文件使用文本搜索')
    if language=='python':
        try:tree=ast.parse(source.decode('utf-8'),filename=path)
        except (SyntaxError,ValueError,RecursionError) as exc:
            raise DevError('CODE_SYNTAX_ERROR','Python 源码不能完整解析，未伪造结构结果') from exc
        symbols=[];references=[];pending=[(tree,())];visited=0
        while pending:
            node,parents=pending.pop();visited+=1
            if visited>MAX_NODES:raise DevError('CODE_ANALYSIS_LIMIT','源码结构超过节点预算，请缩小文件')
            child_parents=parents
            if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                name=node.name;qualified='.'.join((*parents,name));child_parents=(*parents,name)
                kind='class' if isinstance(node,ast.ClassDef) else 'async_function' if isinstance(node,ast.AsyncFunctionDef) else 'function'
                append(symbols,{'name':name,'qualified_name':qualified,'kind':kind,'line':node.lineno,
                    'end_line':node.end_lineno or node.lineno,'column':node.col_offset+1,'container':'.'.join(parents)})
            if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Load):
                append(references,{'name':node.id,'kind':'name_reference','line':node.lineno,'column':node.col_offset+1,'container':'.'.join(parents)})
            elif isinstance(node,ast.Attribute) and isinstance(node.ctx,ast.Load):
                append(references,{'name':node.attr,'kind':'attribute_reference','line':node.lineno,'column':node.col_offset+1,'container':'.'.join(parents)})
            elif isinstance(node,(ast.Import,ast.ImportFrom)):
                for item in node.names:
                    append(references,{'name':item.name,'kind':'import_reference','line':node.lineno,'column':node.col_offset+1,'container':'.'.join(parents)})
            pending.extend((child,child_parents) for child in reversed(list(ast.iter_child_nodes(node))))
        backend='python.ast'
    else:
        try:
            from tree_sitter import Language,Parser
            if language=='javascript':
                import tree_sitter_javascript as grammar
                lang=Language(grammar.language())
            else:
                import tree_sitter_typescript as grammar
                lang=Language(grammar.language_tsx() if language=='tsx' else grammar.language_typescript())
            parser=Parser(lang);tree=parser.parse(source)
        except ImportError as exc:
            raise DevError('PARSER_UNAVAILABLE','请按 requirements-agent.txt 安装对应的 Tree-sitter 解析依赖') from exc
        if tree.root_node.has_error:
            raise DevError('CODE_SYNTAX_ERROR','JavaScript/TypeScript 源码包含语法错误，未将恢复语法树当作完整结果')
        symbols=[];references=[];pending=[(tree.root_node,())];visited=0
        definitions={'function_declaration':'function','generator_function_declaration':'generator_function',
                     'class_declaration':'class','abstract_class_declaration':'class','interface_declaration':'interface',
                     'type_alias_declaration':'type','enum_declaration':'enum','method_definition':'method',
                     'method_signature':'method_signature','function_signature':'function_signature'}
        while pending:
            node,parents=pending.pop();visited+=1
            if visited>MAX_NODES:raise DevError('CODE_ANALYSIS_LIMIT','源码结构超过节点预算，请缩小文件')
            name_node=node.child_by_field_name('name');kind=definitions.get(node.type)
            if node.type=='variable_declarator':
                value=node.child_by_field_name('value')
                if value and value.type in {'arrow_function','function_expression','generator_function','class'}:
                    kind='class' if value.type=='class' else 'function'
            child_parents=parents
            if kind and name_node:
                name=source[name_node.start_byte:name_node.end_byte].decode('utf-8')[:300]
                qualified='.'.join((*parents,name));child_parents=(*parents,name)
                append(symbols,{'name':name,'qualified_name':qualified,'kind':kind,'line':node.start_point.row+1,
                                'end_line':node.end_point.row+1,'column':node.start_point.column+1,'container':'.'.join(parents)})
            if node.type in {'identifier','property_identifier','type_identifier','shorthand_property_identifier'}:
                parent=node.parent
                is_declaration=parent is not None and parent.child_by_field_name('name')==node and (
                    parent.type in definitions or parent.type in {'variable_declarator','required_parameter','optional_parameter','public_field_definition'})
                if not is_declaration:
                    name=source[node.start_byte:node.end_byte].decode('utf-8')[:300]
                    append(references,{'name':name,'kind':'syntactic_reference','line':node.start_point.row+1,
                                       'column':node.start_point.column+1,'container':'.'.join(parents)})
            pending.extend((child,child_parents) for child in reversed(node.named_children))
        backend='tree-sitter'
    symbols.sort(key=lambda s:(s['line'],s['column'],s['name']))
    references.sort(key=lambda s:(s['line'],s['column'],s['name']))
    return {'symbols':symbols,'references':references,'language':language,'backend':backend,
            'precision':'syntax-only; reference candidates may include unrelated same-name identifiers, no cross-file type resolution',
            'column_unit':'one-based UTF-8 byte offset'}


def code_symbols(engine,project,args):
    root,_=engine.root(project)
    path=engine.path(root,args['path'],False)
    data=engine.read_bytes(path);engine.text(data)
    parsed=analyze(args['path'],data)
    query=args['query'].casefold()
    symbols=[s for s in parsed['symbols'] if not query or query in s['qualified_name'].casefold()]
    start=args['offset'];end=start+args['limit']
    return {k:parsed[k] for k in ('language','backend','precision','column_unit')}|{
        'path':args['path'],'sha256':digest(data),'symbols':symbols[start:end],
        'total_symbols':len(symbols),'truncated':end<len(symbols),'next_offset':end if end<len(symbols) else None}
