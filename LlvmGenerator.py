#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    OpenGEODE - A tiny SDL Editor for TASTE

    This module generates LLVM IR code from SDL process models, allowing
    generation of a binary application without an intermediate language.
    LLVM also allows for various code verification, analysis, and optimization.

    The design is based on the Ada code generator. Check it for details.

    Copyright (c) 2012-2013 European Space Agency

    Designed and implemented by Maxime Perrotin

    Contact: maxime.perrotin@esa.int
"""

import logging
from singledispatch import singledispatch
from llvm import core

import ogAST
import Helper

LOG = logging.getLogger(__name__)

__all__ = ['generate']


# Global state
g = None


class GlobalState():
    def __init__(self, process):
        self.name = str(process.processName)
        self.module = core.Module.new(self.name)
        self.dataview = process.dataview

        self.scope = {}
        self.states = {}
        self.structs = {}
        self.strings = {}

        # Initialize built-in types
        self.i1 = core.Type.int(1)
        self.i8 = core.Type.int(8)
        self.i32 = core.Type.int(32)
        self.i64 = core.Type.int(64)
        self.void = core.Type.void()
        self.double = core.Type.double()
        self.i1_ptr = core.Type.pointer(self.i1)
        self.i8_ptr = core.Type.pointer(self.i8)
        self.i32_ptr = core.Type.pointer(self.i32)
        self.i64_ptr = core.Type.pointer(self.i64)
        self.double_ptr = core.Type.pointer(self.double)

        # Intialize built-in functions
        ty = core.Type.function(self.void, [core.Type.pointer(self.i8)], True)
        self.printf = self.module.add_function(ty, 'printf')

        self.memcpy = core.Function.intrinsic(
            self.module, core.INTR_MEMCPY,
            [self.i8_ptr, self.i8_ptr, self.i64]
        )


class Struct():
    def __init__(self, name, fields):
        self.name = name
        self.fields = fields
        self.field_names = [n for n, _ in self.fields]
        self.ty = core.Type.struct([ty for _, ty in self.fields], self.name)

    def idx(self, name):
        return self.field_names.index(name)


@singledispatch
def generate(ast):
    ''' Generate the code for an item of the AST '''
    raise TypeError('[Backend] Unsupported AST construct')


# Processing of the AST
@generate.register(ogAST.Process)
def _process(process):
    ''' Generate LLVM IR code '''
    LOG.info('Generating LLVM IR code for process ' + str(process.processName))

    global g
    g = GlobalState(process)

    # In case model has nested states, flatten everything
    Helper.flatten(process)

    # Make an maping {input: {state: transition...}} in order to easily
    # generate the lookup tables for the state machine runtime
    mapping = Helper.map_input_state(process)

    # Initialize states enum
    for name in process.mapping.iterkeys():
        if not name.endswith('START'):
            cons = core.Constant.int(g.i32, len(g.states))
            g.states[name] = cons

    # Generate state var
    state_cons = g.module.add_global_variable(g.i32, 'state')
    state_cons.initializer = core.Constant.int(g.i32, -1)

    # Initialize output signals
    for signal in process.output_signals:
        param_tys = [core.Type.pointer(_generate_type(signal['type']))]
        func_ty = core.Type.function(g.void, param_tys)
        core.Function.new(g.module, func_ty, str(signal['name']))

    # Initialize external procedures
    for proc in [proc for proc in process.procedures if proc.external]:
        param_tys = [core.Type.pointer(_generate_type(p['type'])) for p in proc.fpar]
        func_ty = core.Type.function(g.void, param_tys)
        core.Function.new(g.module, func_ty, str(proc.inputString))

    # Generare process-level vars
    for var_name, (var_asn1_type, def_value) in process.variables.viewitems():
        var_type = _generate_type(var_asn1_type)
        global_var = g.module.add_global_variable(var_type, str(var_name).lower())
        global_var.initializer = core.Constant.null(var_type)

        if def_value:
            raise NotImplementedError

    # Generate process functions
    g.runtr = _generate_runtr_func(process)
    _generate_startup_func(process)

    # Generate input signals
    for signal in process.input_signals:
        _generate_input_signal(signal, mapping[signal['name']])

    g.module.verify()

    with open(g.name  + '.ll', 'w') as ll_file:
        ll_file.write(str(g.module))


def _generate_runtr_func(process):
    ''' Generate code for the run_transition function '''
    func_name = 'run_transition'
    func_type = core.Type.function(g.void, [g.i32])
    func = core.Function.new(g.module, func_type, func_name)

    entry_block = func.append_basic_block('entry')
    cond_block = func.append_basic_block('cond')
    body_block = func.append_basic_block('body')
    exit_block = func.append_basic_block('exit')

    g.builder = core.Builder.new(entry_block)

    # entry
    id_ptr = g.builder.alloca(g.i32, None, 'id')
    g.scope['id'] = id_ptr
    g.builder.store(func.args[0], id_ptr)
    g.builder.branch(cond_block)

    # cond
    g.builder.position_at_end(cond_block)
    no_tr_cons = core.Constant.int(g.i32, -1)
    id_val = g.builder.load(id_ptr)
    cond_val = g.builder.icmp(core.ICMP_NE, id_val, no_tr_cons, 'cond')
    g.builder.cbranch(cond_val, body_block, exit_block)

    # body
    g.builder.position_at_end(body_block)
    switch = g.builder.switch(func.args[0], exit_block)

    # transitions
    for idx, tr in enumerate(process.transitions):
        tr_block = func.append_basic_block('tr%d' % idx)
        const = core.Constant.int(g.i32, idx)
        switch.add_case(const, tr_block)
        g.builder.position_at_end(tr_block)
        generate(tr)
        g.builder.branch(cond_block)

    # exit
    g.builder.position_at_end(exit_block)
    g.builder.ret_void()

    func.verify()
    g.scope.clear()
    return func


def _generate_startup_func(process):
    ''' Generate code for the startup function '''
    func_name = g.name + '_startup'
    func_type = core.Type.function(g.void, [])
    func = core.Function.new(g.module, func_type, func_name)

    entry_block = func.append_basic_block('entry')
    builder = core.Builder.new(entry_block)
    g.builder = builder

    # entry
    builder.call(g.runtr, [core.Constant.int(core.Type.int(), 0)])
    builder.ret_void()

    func.verify()
    return func


def _generate_input_signal(signal, inputs):
    ''' Generate code for an input signal '''
    func_name = g.name + "_" + str(signal['name'])
    param_tys = []
    if 'type' in signal:
        param_tys.append(core.Type.pointer(_generate_type(signal['type'])))
    func_type = core.Type.function(g.void, param_tys)
    func = core.Function.new(g.module, func_type, func_name)

    entry_block = func.append_basic_block('entry')
    exit_block = func.append_basic_block('exit')
    g.builder = core.Builder.new(entry_block)

    runtr_func = g.module.get_function_named('run_transition')

    g_state_val = g.builder.load(g.module.get_global_variable_named('state'))
    switch = g.builder.switch(g_state_val, exit_block)

    for state_name, state_id in g.states.iteritems():
        state_block = func.append_basic_block('state_%s' % str(state_name))
        switch.add_case(state_id, state_block)
        g.builder.position_at_end(state_block)

        # TODO: Nested states

        input = inputs.get(state_name)
        if input:
            for var_name in input.parameters:
                var_val = g.module.get_global_variable_named(str(var_name).lower())
                _generate_assign(var_val, func.args[0])
            if input.transition:
                id_val = core.Constant.int(g.i32, input.transition_id)
                g.builder.call(runtr_func, [id_val])

        g.builder.ret_void()

    g.builder.position_at_end(exit_block)
    g.builder.ret_void()

    func.verify()


@generate.register(ogAST.Output)
@generate.register(ogAST.ProcedureCall)
def _call_external_function(output):
    ''' Generate the code of a set of output or procedure call statement '''

    for out in output.output:
        name = out['outputName'].lower()

        if name == 'write':
            _generate_write(out['params'])
            continue
        elif name == 'writeln':
            _generate_writeln(out['params'])
            continue
        elif name == 'reset_timer':
            _generate_reset_timer(out['params'])
            continue
        elif name == 'set_timer':
            _generate_set_timer(out['params'])
            continue

        func = g.module.get_function_named(str(name))
        g.builder.call(func, [expression(p) for p in out.get('params', [])])


def _generate_write(params):
    ''' Generate the code for the write operator '''
    zero = core.Constant.int(g.i32, 0)
    for param in params:
        basic_ty = find_basic_type(param.exprType)
        expr_val = expression(param)
        if basic_ty.kind == 'IntegerType':
            fmt_val = _get_string_cons('% d')
            fmt_ptr = g.builder.gep(fmt_val, [zero, zero])
            g.builder.call(g.printf, [fmt_ptr, expr_val])
        elif basic_ty.kind == 'RealType':
            fmt_val = _get_string_cons('% .14E')
            fmt_ptr = g.builder.gep(fmt_val, [zero, zero])
            g.builder.call(g.printf, [fmt_ptr, expr_val])
        elif basic_ty.kind == 'BooleanType':
            true_str_val = _get_string_cons('TRUE')
            true_str_ptr = g.builder.gep(true_str_val, [zero, zero])
            false_str_val = _get_string_cons('FALSE')
            false_str_ptr = g.builder.gep(false_str_val, [zero, zero])
            str_ptr = g.builder.select(expr_val, true_str_ptr, false_str_ptr)
            g.builder.call(g.printf, [str_ptr])
        else:
            raise NotImplementedError


def _generate_writeln(params):
    ''' Generate the code for the writeln operator '''
    _generate_write(params)

    zero = core.Constant.int(g.i32, 0)
    str_cons = _get_string_cons('\n')
    str_ptr = g.builder.gep(str_cons, [zero, zero])
    g.builder.call(g.printf, [str_ptr])


def _generate_reset_timer(params):
    ''' Generate the code for the reset timer operator '''
    raise NotImplementedError


def _generate_set_timer(params):
    ''' Generate the code for the set timer operator '''
    raise NotImplementedError


@generate.register(ogAST.TaskAssign)
def _task_assign(task):
    ''' Generate the code of a list of assignments '''
    for expr in task.elems:
        expression(expr)


@generate.register(ogAST.TaskInformalText)
def _task_informal_text(task):
    ''' Generate comments for informal text '''
    raise NotImplementedError


@generate.register(ogAST.TaskForLoop)
def _task_forloop(task):
    ''' Generate the code for a for loop '''
    raise NotImplementedError


# ------ expressions --------

@singledispatch
def expression(expr):
    ''' Generate the code for Expression-classes '''
    raise TypeError('Unsupported expression: ' + str(expr))


@expression.register(ogAST.PrimVariable)
def _primary_variable(prim):
    ''' Generate the code for a single variable reference '''
    return g.module.get_global_variable_named(str(prim.value[0]).lower())


@expression.register(ogAST.PrimPath)
def _prim_path(primary_id):
    ''' Generate the code for an of an element list (path) '''
    raise NotImplementedError


@expression.register(ogAST.ExprPlus)
@expression.register(ogAST.ExprMul)
@expression.register(ogAST.ExprMinus)
@expression.register(ogAST.ExprEq)
@expression.register(ogAST.ExprNeq)
@expression.register(ogAST.ExprGt)
@expression.register(ogAST.ExprGe)
@expression.register(ogAST.ExprLt)
@expression.register(ogAST.ExprLe)
@expression.register(ogAST.ExprDiv)
@expression.register(ogAST.ExprMod)
@expression.register(ogAST.ExprRem)
def _basic(expr):
    ''' Generate the code for an arithmetic of relational expression '''
    lefttmp = expression(expr.left)
    righttmp = expression(expr.right)

    # load the value of the expression if it is a pointer
    if lefttmp.type.kind == core.TYPE_POINTER:
        lefttmp = g.builder.load(lefttmp, 'lefttmp')
    if righttmp.type.kind == core.TYPE_POINTER:
        righttmp = g.builder.load(righttmp, 'righttmp')

    if lefttmp.type.kind != righttmp.type.kind:
        raise NotImplementedError

    if lefttmp.type.kind == core.TYPE_INTEGER:
        if expr.operand == '+':
            return g.builder.add(lefttmp, righttmp, 'addtmp')
        elif expr.operand == '-':
            return g.builder.sub(lefttmp, righttmp, 'subtmp')
        elif expr.operand == '*':
            return g.builder.mul(lefttmp, righttmp, 'multmp')
        elif expr.operand == '/':
            return g.builder.sdiv(lefttmp, righttmp, 'divtmp')
        elif expr.operand == 'mod':
            # l mod r == (((l rem r) + r) rem r)
            remtmp = g.builder.srem(lefttmp, righttmp)
            addtmp = g.builder.add(remtmp, righttmp)
            return g.builder.srem(addtmp, righttmp, 'modtmp')
        elif expr.operand == 'rem':
            return g.builder.srem(lefttmp, righttmp, 'remtmp')
        elif expr.operand == '<':
            return g.builder.icmp(core.ICMP_SLT, lefttmp, righttmp, 'lttmp')
        elif expr.operand == '<=':
            return g.builder.icmp(core.ICMP_SLE, lefttmp, righttmp, 'letmp')
        elif expr.operand == '=':
            return g.builder.icmp(core.ICMP_EQ, lefttmp, righttmp, 'eqtmp')
        elif expr.operand == '/=':
            return g.builder.icmp(core.ICMP_NE, lefttmp, righttmp, 'netmp')
        elif expr.operand == '>=':
            return g.builder.icmp(core.ICMP_SGE, lefttmp, righttmp, 'getmp')
        elif expr.operand == '>':
            return g.builder.icmp(core.ICMP_SGT, lefttmp, righttmp, 'gttmp')
        else:
            raise NotImplementedError
    elif lefttmp.type.kind == core.TYPE_DOUBLE:
        if expr.operand == '+':
            return g.builder.fadd(lefttmp, righttmp, 'addtmp')
        elif expr.operand == '-':
            return g.builder.fsub(lefttmp, righttmp, 'subtmp')
        elif expr.operand == '*':
            return g.builder.fmul(lefttmp, righttmp, 'multmp')
        elif expr.operand == '/':
            return g.builder.fdiv(lefttmp, righttmp, 'divtmp')
        elif expr.operand == '<':
            return g.builder.fcmp(core.FCMP_OLT, lefttmp, righttmp, 'lttmp')
        elif expr.operand == '<=':
            return g.builder.fcmp(core.FCMP_OLE, lefttmp, righttmp, 'letmp')
        elif expr.operand == '=':
            return g.builder.fcmp(core.FCMP_OEQ, lefttmp, righttmp, 'eqtmp')
        elif expr.operand == '/=':
            return g.builder.fcmp(core.FCMP_ONE, lefttmp, righttmp, 'netmp')
        elif expr.operand == '>=':
            return g.builder.fcmp(core.FCMP_OGE, lefttmp, righttmp, 'getmp')
        elif expr.operand == '>':
            return g.builder.fcmp(core.FCMP_OGT, lefttmp, righttmp, 'gttmp')
        else:
            raise NotImplementedError
    else:
        raise NotImplementedError


@expression.register(ogAST.ExprAssign)
def _assign(expr):
    ''' Generate the code for an assign expression '''
    left = expression(expr.left)
    right = expression(expr.right)
    _generate_assign(left, right)
    return left


def _generate_assign(left, right):
    ''' Generate code for an assign from two LLVM values'''
    # This is extracted as an standalone function because is used by
    # multiple generation rules
    if left.type.kind == core.TYPE_POINTER and left.type.pointee.kind == core.TYPE_STRUCT:
        size = core.Constant.int(g.i64, 2)
        align = core.Constant.int(g.i32, 1)
        volatile = core.Constant.int(g.i1, 0)

        right_ptr = g.builder.bitcast(right, g.i8_ptr)
        left_ptr = g.builder.bitcast(left, g.i8_ptr)

        g.builder.call(g.memcpy, [left_ptr, right_ptr, size, align, volatile])
    else:
        g.builder.store(right, left)


@expression.register(ogAST.ExprOr)
@expression.register(ogAST.ExprAnd)
@expression.register(ogAST.ExprXor)
def _logical(expr):
    ''' Generate the code for a logical expression '''
    lefttmp = expression(expr.left)
    righttmp = expression(expr.right)

    ty = find_basic_type(expr.exprType)
    if ty.kind != 'BooleanType':
        raise NotImplementedError

    # load the value of the expression if it is a pointer
    if lefttmp.type.kind == core.TYPE_POINTER:
        lefttmp = g.builder.load(lefttmp, 'lefttmp')
    if righttmp.type.kind == core.TYPE_POINTER:
        righttmp = g.builder.load(righttmp, 'righttmp')

    if expr.operand == 'and':
        return g.builder.and_(lefttmp, righttmp, 'andtmp')
    elif expr.operand == 'or':
        return g.builder.or_(lefttmp, righttmp, 'ortmp')
    else:
        return g.builder.xor(lefttmp, righttmp, 'xortmp')


@expression.register(ogAST.ExprAppend)
def _append(expr):
    ''' Generate code for the APPEND construct: a // b '''
    raise NotImplementedError


@expression.register(ogAST.ExprIn)
def _expr_in(expr):
    ''' Generate the code for an in expression '''
    raise NotImplementedError


@expression.register(ogAST.PrimEnumeratedValue)
def _enumerated_value(primary):
    ''' Generate code for an enumerated value '''
    raise NotImplementedError


@expression.register(ogAST.PrimChoiceDeterminant)
def _choice_determinant(primary):
    ''' Generate code for a choice determinant (enumerated) '''
    raise NotImplementedError


@expression.register(ogAST.PrimInteger)
def _integer(primary):
    ''' Generate code for a raw integer value  '''
    return core.Constant.int(g.i32, primary.value[0])


@expression.register(ogAST.PrimReal)
def _real(primary):
    ''' Generate code for a raw real value  '''
    return core.Constant.real(g.double, primary.value[0])


@expression.register(ogAST.PrimBoolean)
def _boolean(primary):
    ''' Generate code for a raw boolean value  '''
    if primary.value[0].lower() == 'true':
        return core.Constant.int(g.i1, 1)
    else:
        return core.Constant.int(g.i1, 0)


@expression.register(ogAST.PrimEmptyString)
def _empty_string(primary):
    ''' Generate code for an empty SEQUENCE OF: {} '''
    raise NotImplementedError


@expression.register(ogAST.PrimStringLiteral)
def _string_literal(primary):
    ''' Generate code for a string (Octet String) '''
    raise NotImplementedError


@expression.register(ogAST.PrimConstant)
def _constant(primary):
    ''' Generate code for a reference to an ASN.1 constant '''
    raise NotImplementedError


@expression.register(ogAST.PrimMantissaBaseExp)
def _mantissa_base_exp(primary):
    ''' Generate code for a Real with Mantissa-base-Exponent representation '''
    raise NotImplementedError


@expression.register(ogAST.PrimIfThenElse)
def _if_then_else(ifthen):
    ''' Generate the code for ternary operator '''
    raise NotImplementedError


@expression.register(ogAST.PrimSequence)
def _sequence(seq):
    ''' Generate the code for an ASN.1 SEQUENCE '''
    struct = g.structs[seq.exprType.ReferencedTypeName]
    struct_ptr = g.builder.alloca(struct.ty)
    zero_cons = core.Constant.int(g.i32, 0)

    for field_name, field_expr in seq.value.viewitems():
        field_val = expression(field_expr)
        field_idx_cons = core.Constant.int(g.i32, struct.idx(field_name))
        field_ptr = g.builder.gep(struct_ptr, [zero_cons, field_idx_cons])
        g.builder.store(field_val, field_ptr)

    return struct_ptr


@expression.register(ogAST.PrimSequenceOf)
def _sequence_of(seqof):
    ''' Generate the code for an ASN.1 SEQUENCE OF '''
    ty = _generate_type(seqof.exprType)
    struct_ptr = g.builder.alloca(ty)
    zero_cons = core.Constant.int(g.i32, 0)
    array_ptr = g.builder.gep(struct_ptr, [zero_cons, zero_cons])

    for idx, expr in enumerate(seqof.value):
        idx_cons = core.Constant.int(g.i32, idx)
        expr_val = expression(expr)
        pos_ptr = g.builder.gep(array_ptr, [zero_cons, idx_cons])
        g.builder.store(expr_val, pos_ptr)

    return struct_ptr


@expression.register(ogAST.PrimChoiceItem)
def _choiceitem(choice):
    ''' Generate the code for a CHOICE expression '''
    raise NotImplementedError


@generate.register(ogAST.Decision)
def _decision(dec):
    ''' Generate the code for a decision '''
    func = g.builder.basic_block.function

    ans_cond_blocks = [func.append_basic_block('ans_cond') for ans in dec.answers]
    end_block = func.append_basic_block('end')

    g.builder.branch(ans_cond_blocks[0])

    for idx, ans in enumerate(dec.answers):
        ans_cond_block = ans_cond_blocks[idx]
        if ans.transition:
            ans_tr_block = func.append_basic_block('ans_tr')
        g.builder.position_at_end(ans_cond_block)

        if ans.kind == 'constant':
            next_block = ans_cond_blocks[idx+1] if idx < len(ans_cond_blocks) else end_block

            expr = ans.openRangeOp()
            expr.left = dec.question
            expr.right = ans.constant
            expr_val = expression(expr)

            true_cons = core.Constant.int(g.i1, 1)
            cond_val = g.builder.icmp(core.ICMP_EQ, expr_val, true_cons)
            g.builder.cbranch(cond_val, ans_tr_block if ans.transition else end_block, next_block)
        elif ans.kind == 'else':
            if ans.transition:
                g.builder.branch(ans_tr_block)
            else:
                g.builder.branch(end_block)
        else:
            raise NotImplementedError

        if ans.transition:
            g.builder.position_at_end(ans_tr_block)
            generate(ans.transition)
            g.builder.branch(end_block)

    g.builder.position_at_end(end_block)


@generate.register(ogAST.Label)
def _label(tr):
    ''' TGenerate the code for a Label '''
    raise NotImplementedError


@generate.register(ogAST.Transition)
def _transition(tr):
    ''' Generate the code for a transition '''
    for action in tr.actions:
        generate(action)
        if isinstance(action, ogAST.Label):
            return
    if tr.terminator:
        _generate_terminator(tr.terminator)


def _generate_terminator(term):
    ''' Generate the code for a transition termiantor '''
    id_ptr = g.scope['id']
    if term.label:
        raise NotImplementedError
    if term.kind == 'next_state':
        state = term.inputString.lower()
        if state.strip() != '-':
            next_id_cons = core.Constant.int(g.i32, term.next_id)
            g.builder.store(next_id_cons, id_ptr)
            if term.next_id == -1:
                state_ptr = g.module.get_global_variable_named('state')
                state_id_cons = g.states[state]
                g.builder.store(state_id_cons, state_ptr)
        else:
            raise NotImplementedError
    elif term.kind == 'join':
        raise NotImplementedError
    elif term.kind == 'stop':
        raise NotImplementedError
    elif term.kind == 'return':
        raise NotImplementedError


@generate.register(ogAST.Floating_label)
def _floating_label(label):
    ''' Generate the code for a floating label '''
    raise NotImplementedError


@generate.register(ogAST.Procedure)
def _inner_procedure(proc):
    ''' Generate the code for a procedure '''
    raise NotImplementedError


def _generate_type(ty):
    ''' Generate the equivalent LLVM type of a ASN.1 type '''
    basic_ty = find_basic_type(ty)
    if basic_ty.kind == 'IntegerType':
        return g.i32
    elif basic_ty.kind == 'BooleanType':
        return g.i1
    elif basic_ty.kind == 'RealType':
        return g.double
    elif basic_ty.kind == 'SequenceOfType':
        if ty.ReferencedTypeName in g.structs:
            return g.structs[ty.ReferencedTypeName].ty

        min_size = int(basic_ty.Max)
        max_size = int(basic_ty.Min)
        if min_size != max_size:
            raise NotImplementedError

        elem_ty = _generate_type(basic_ty.type)
        array_ty = core.Type.array(elem_ty, max_size)
        struct = Struct(ty.ReferencedTypeName, ['_', array_ty])
        g.structs[ty.ReferencedTypeName] = struct
        return struct.ty
    elif basic_ty.kind == 'SequenceType':
        if ty.ReferencedTypeName in g.structs:
            return g.structs[ty.ReferencedTypeName].ty
        # TODO: Fields should be iterated in the same order as defined in the type
        fields = [[n, _generate_type(f.type)] for n, f in basic_ty.Children.viewitems()]
        struct = Struct(ty.ReferencedTypeName, fields)
        g.structs[ty.ReferencedTypeName] = struct
        return struct.ty
    else:
        raise NotImplementedError


def _get_string_cons(str):
    ''' Returns a reference to a global string constant with the given value '''
    if str in g.strings:
        return g.strings[str]

    str_val = core.Constant.stringz(str)
    # TODO: This names can cause conflicts with user defined variables
    gvar_name = 'str_%s' % len(g.strings)
    gvar_val = g.module.add_global_variable(str_val.type, gvar_name)
    gvar_val.initializer = str_val
    g.strings[str] = gvar_val
    return gvar_val


# TODO: Refactor this into the helper module
def find_basic_type(a_type):
    ''' Return the ASN.1 basic type of a_type '''
    basic_type = a_type
    while basic_type.kind == 'ReferenceType':
        # Find type with proper case in the data view
        for typename in g.dataview.viewkeys():
            if typename.lower() == basic_type.ReferencedTypeName.lower():
                basic_type = g.dataview[typename].type
                break
    return basic_type
