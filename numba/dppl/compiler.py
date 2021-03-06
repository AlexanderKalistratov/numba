from __future__ import print_function, absolute_import
import copy
from collections import namedtuple

from .dppl_passbuilder import DPPLPassBuilder
from numba.core.typing.templates import ConcreteTemplate
from numba.core import types, compiler, ir
from numba.core.typing.templates import AbstractTemplate
import ctypes
from types import FunctionType
from inspect import signature

import dppl.ocldrv as driver
from . import spirv_generator

import os
from numba.core.compiler import DefaultPassBuilder, CompilerBase

DEBUG=os.environ.get('NUMBA_DPPL_DEBUG', None)
_NUMBA_DPPL_READ_ONLY  = "read_only"
_NUMBA_DPPL_WRITE_ONLY = "write_only"
_NUMBA_DPPL_READ_WRITE = "read_write"

def _raise_no_device_found_error():
    error_message = ("No OpenCL device specified. "
                     "Usage : jit_fn[device, globalsize, localsize](...)")
    raise ValueError(error_message)

def _raise_invalid_kernel_enqueue_args():
    error_message = ("Incorrect number of arguments for enquing dppl.kernel. "
                     "Usage: device_env, global size, local size. "
                     "The local size argument is optional.")
    raise ValueError(error_message)


def get_ordered_arg_access_types(pyfunc, access_types):
    # Construct a list of access type of each arg according to their position
    ordered_arg_access_types = []
    sig = signature(pyfunc, follow_wrapped=False)
    for idx, arg_name in enumerate(sig.parameters):
        if access_types:
            for key in access_types:
                if arg_name in access_types[key]:
                    ordered_arg_access_types.append(key)
        if len(ordered_arg_access_types) <= idx:
            ordered_arg_access_types.append(None)

    return ordered_arg_access_types

class DPPLCompiler(CompilerBase):
    """ DPPL Compiler """

    def define_pipelines(self):
        # this maintains the objmode fallback behaviour
        pms = []
        if not self.state.flags.force_pyobject:
            #print("Numba-DPPL [INFO]: Using Numba-DPPL pipeline")
            pms.append(DPPLPassBuilder.define_nopython_pipeline(self.state))
        if self.state.status.can_fallback or self.state.flags.force_pyobject:
            pms.append(
                DefaultPassBuilder.define_objectmode_pipeline(self.state)
            )
        if self.state.status.can_giveup:
            pms.append(
                DefaultPassBuilder.define_interpreted_pipeline(self.state)
            )
        return pms


def compile_with_dppl(pyfunc, return_type, args, debug):
    # First compilation will trigger the initialization of the OpenCL backend.
    from .descriptor import dppl_target

    typingctx = dppl_target.typing_context
    targetctx = dppl_target.target_context
    # TODO handle debug flag
    flags = compiler.Flags()
    # Do not compile (generate native code), just lower (to LLVM)
    flags.set('no_compile')
    flags.set('no_cpython_wrapper')
    flags.unset('nrt')

    # Run compilation pipeline
    if isinstance(pyfunc, FunctionType):
        cres = compiler.compile_extra(typingctx=typingctx,
                                      targetctx=targetctx,
                                      func=pyfunc,
                                      args=args,
                                      return_type=return_type,
                                      flags=flags,
                                      locals={},
                                      pipeline_class=DPPLCompiler)
    elif isinstance(pyfunc, ir.FunctionIR):
        cres = compiler.compile_ir(typingctx=typingctx,
                                   targetctx=targetctx,
                                   func_ir=pyfunc,
                                   args=args,
                                   return_type=return_type,
                                   flags=flags,
                                   locals={},
                                   pipeline_class=DPPLCompiler)
    else:
        assert(0)
    # Linking depending libraries
    # targetctx.link_dependencies(cres.llvm_module, cres.target_context.linking)
    library = cres.library
    library.finalize()

    return cres


def compile_kernel(device, pyfunc, args, access_types, debug=False):
    if DEBUG:
        print("compile_kernel", args)
    if not device:
        device = driver.runtime.get_current_device()

    cres = compile_with_dppl(pyfunc, None, args, debug=debug)
    func = cres.library.get_function(cres.fndesc.llvm_func_name)
    kernel = cres.target_context.prepare_ocl_kernel(func, cres.signature.args)
    oclkern = DPPLKernel(device_env=device,
                         llvm_module=kernel.module,
                         name=kernel.name,
                         argtypes=cres.signature.args,
                         ordered_arg_access_types=access_types)
    return oclkern


def compile_kernel_parfor(device, func_ir, args, args_with_addrspaces,
                          debug=False):
    if DEBUG:
        print("compile_kernel_parfor", args)
        for a in args:
            print(a, type(a))
            if isinstance(a, types.npytypes.Array):
                print("addrspace:", a.addrspace)

    cres = compile_with_dppl(func_ir, None, args_with_addrspaces,
                             debug=debug)
    #cres = compile_with_dppl(func_ir, types.void, args, debug=debug)
    func = cres.library.get_function(cres.fndesc.llvm_func_name)

    if DEBUG:
        print("compile_kernel_parfor signature", cres.signature.args)
        for a in cres.signature.args:
            print(a, type(a))
#            if isinstance(a, types.npytypes.Array):
#                print("addrspace:", a.addrspace)

    kernel = cres.target_context.prepare_ocl_kernel(func, cres.signature.args)
    #kernel = cres.target_context.prepare_ocl_kernel(func, args_with_addrspaces)
    oclkern = DPPLKernel(device_env=device,
                         llvm_module=kernel.module,
                         name=kernel.name,
                         argtypes=args_with_addrspaces)
                         #argtypes=cres.signature.args)
    return oclkern


def compile_dppl_func(pyfunc, return_type, args, debug=False):
    cres = compile_with_dppl(pyfunc, return_type, args, debug=debug)
    func = cres.library.get_function(cres.fndesc.llvm_func_name)
    cres.target_context.mark_ocl_device(func)
    devfn = DPPLFunction(cres)

    class dppl_function_template(ConcreteTemplate):
        key = devfn
        cases = [cres.signature]

    cres.typing_context.insert_user_function(devfn, dppl_function_template)
    libs = [cres.library]
    cres.target_context.insert_user_function(devfn, cres.fndesc, libs)
    return devfn


# Compile dppl function template
def compile_dppl_func_template(pyfunc):
    """Compile a DPPLFunctionTemplate
    """
    from .descriptor import dppl_target

    dft = DPPLFunctionTemplate(pyfunc)

    class dppl_function_template(AbstractTemplate):
        key = dft

        def generic(self, args, kws):
            assert not kws
            return dft.compile(args)

    typingctx = dppl_target.typing_context
    typingctx.insert_user_function(dft, dppl_function_template)
    return dft


class DPPLFunctionTemplate(object):
    """Unmaterialized dppl function
    """
    def __init__(self, pyfunc, debug=False):
        self.py_func = pyfunc
        self.debug = debug
        # self.inline = inline
        self._compileinfos = {}

    def compile(self, args):
        """Compile the function for the given argument types.

        Each signature is compiled once by caching the compiled function inside
        this object.
        """
        if args not in self._compileinfos:
            cres = compile_with_dppl(self.py_func, None, args, debug=self.debug)
            func = cres.library.get_function(cres.fndesc.llvm_func_name)
            cres.target_context.mark_ocl_device(func)
            first_definition = not self._compileinfos
            self._compileinfos[args] = cres
            libs = [cres.library]

            if first_definition:
                # First definition
                cres.target_context.insert_user_function(self, cres.fndesc,
                                                         libs)
            else:
                cres.target_context.add_user_function(self, cres.fndesc, libs)

        else:
            cres = self._compileinfos[args]

        return cres.signature


class DPPLFunction(object):
    def __init__(self, cres):
        self.cres = cres


def _ensure_valid_work_item_grid(val, device_env):

    if not isinstance(val, (tuple, list, int)):
        error_message = ("Cannot create work item dimension from "
                         "provided argument")
        raise ValueError(error_message)

    if isinstance(val, int):
        val = [val]

    if len(val) > device_env.get_max_work_item_dims():
        error_message = ("Unsupported number of work item dimensions ")
        raise ValueError(error_message)

    return list(val)

def _ensure_valid_work_group_size(val, work_item_grid):

    if not isinstance(val, (tuple, list, int)):
        error_message = ("Cannot create work item dimension from "
                         "provided argument")
        raise ValueError(error_message)

    if isinstance(val, int):
        val = [val]

    if len(val) != len(work_item_grid):
        error_message = ("Unsupported number of work item dimensions, " +
                         "dimensions of global and local work items has to be the same ")
        raise ValueError(error_message)

    return list(val)


class DPPLKernelBase(object):
    """Define interface for configurable kernels
    """

    def __init__(self):
        self.global_size = []
        self.local_size  = []
        self.device_env  = None

        # list of supported access types, stored in dict for fast lookup
        self.valid_access_types = {
                _NUMBA_DPPL_READ_ONLY: _NUMBA_DPPL_READ_ONLY,
                _NUMBA_DPPL_WRITE_ONLY: _NUMBA_DPPL_WRITE_ONLY,
                _NUMBA_DPPL_READ_WRITE: _NUMBA_DPPL_READ_WRITE}

    def copy(self):
        return copy.copy(self)

    def configure(self, device_env, global_size, local_size=None):
        """Configure the OpenCL kernel. The local_size can be None
        """
        clone = self.copy()
        clone.global_size = global_size
        clone.local_size = local_size if local_size else []
        clone.device_env = device_env

        return clone

    def forall(self, nelem, local_size=64, queue=None):
        """Simplified configuration for 1D kernel launch
        """
        return self.configure(nelem, min(nelem, local_size), queue=queue)

    def __getitem__(self, args):
        """Mimick CUDA python's square-bracket notation for configuration.
        This assumes the argument to be:
            `global size, local size`
        """
        ls = None
        nargs = len(args)
        # Check if the kernel enquing arguments are sane
        if nargs < 1 or nargs > 2:
            _raise_invalid_kernel_enqueue_args

        device_env = driver.runtime.get_current_device()
        gs = _ensure_valid_work_item_grid(args[0], device_env)
        # If the optional local size argument is provided
        if nargs == 2 and args[1] != []:
            ls = _ensure_valid_work_group_size(args[1], gs)

        return self.configure(device_env, gs, ls)


class DPPLKernel(DPPLKernelBase):
    """
    A OCL kernel object
    """

    def __init__(self, device_env, llvm_module, name, argtypes,
                 ordered_arg_access_types=None):
        super(DPPLKernel, self).__init__()
        self._llvm_module = llvm_module
        self.assembly = self.binary = llvm_module.__str__()
        self.entry_name = name
        self.argument_types = tuple(argtypes)
        self.ordered_arg_access_types = ordered_arg_access_types
        self._argloc = []
        # First-time compilation using SPIRV-Tools
        if DEBUG:
            with open("llvm_kernel.ll", "w") as f:
                f.write(self.binary)
        self.spirv_bc = spirv_generator.llvm_to_spirv(self.binary)
        # create a program
        self.program = driver.Program(device_env, self.spirv_bc)
        #  create a kernel
        self.kernel = driver.Kernel(device_env, self.program, self.entry_name)

    def __call__(self, *args):

        # Create an array of KenrelArgs
        # Unpack pyobject values into ctypes scalar values
        retr = []  # hold functors for writeback
        kernelargs = []
        internal_device_arrs = []
        for ty, val, access_type in zip(self.argument_types, args,
                                        self.ordered_arg_access_types):
            self._unpack_argument(ty, val, self.device_env, retr,
                    kernelargs, internal_device_arrs, access_type)

        # enqueues the kernel
        driver.enqueue_kernel(self.device_env, self.kernel, kernelargs,
                              self.global_size, self.local_size)

        for ty, val, i_dev_arr, access_type in zip(self.argument_types, args,
                internal_device_arrs, self.ordered_arg_access_types):
            self._pack_argument(ty, val, self.device_env, i_dev_arr,
                                access_type)

    def _pack_argument(self, ty, val, device_env, device_arr, access_type):
        """
        Copy device data back to host
        """
        if (device_arr and (access_type not in self.valid_access_types or
            access_type in self.valid_access_types and
            self.valid_access_types[access_type] != _NUMBA_DPPL_READ_ONLY)):
            # we get the date back to host if have created a
            # device_array or if access_type of this device_array
            # is not of type read_only and read_write
            device_env.copy_array_from_device(device_arr)

    def _unpack_device_array_argument(self, val, kernelargs):
        # this function only takes DeviceArray created for ndarrays
        void_ptr_arg = True
        # meminfo
        kernelargs.append(driver.KernelArg(None, void_ptr_arg))
        # parent
        kernelargs.append(driver.KernelArg(None, void_ptr_arg))
        kernelargs.append(driver.
                          KernelArg(ctypes.c_size_t(val._ndarray.size)))
        kernelargs.append(driver.
                          KernelArg(
                              ctypes.c_size_t(val._ndarray.dtype.itemsize)))
        kernelargs.append(driver.KernelArg(val))
        for ax in range(val._ndarray.ndim):
            kernelargs.append(driver.
                              KernelArg(
                                  ctypes.c_size_t(val._ndarray.shape[ax])))
        for ax in range(val._ndarray.ndim):
            kernelargs.append(driver.
                              KernelArg(
                                  ctypes.c_size_t(val._ndarray.strides[ax])))


    def _unpack_argument(self, ty, val, device_env, retr, kernelargs,
                         device_arrs, access_type):
        """
        Convert arguments to ctypes and append to kernelargs
        """
        device_arrs.append(None)
        if isinstance(val, driver.DeviceArray):
            self._unpack_device_array_argument(val, kernelargs)

        elif isinstance(ty, types.Array):
            default_behavior = self.check_for_invalid_access_type(access_type)
            dArr = None

            if (default_behavior or
                self.valid_access_types[access_type] == _NUMBA_DPPL_READ_ONLY or
                self.valid_access_types[access_type] == _NUMBA_DPPL_READ_WRITE):
                # default, read_only and read_write case
                dArr = device_env.copy_array_to_device(val)
            elif self.valid_access_types[access_type] == _NUMBA_DPPL_WRITE_ONLY:
                # write_only case, we do not copy the host data
                dArr = device_env.create_device_array(val)

            assert (dArr != None), "Problem in allocating device buffer"
            device_arrs[-1] = dArr
            self._unpack_device_array_argument(dArr, kernelargs)

        elif isinstance(ty, types.Integer):
            cval = ctypes.c_size_t(val)
            kernelargs.append(driver.KernelArg(cval))

        elif ty == types.float64:
            cval = ctypes.c_double(val)
            kernelargs.append(driver.KernelArg(cval))

        elif ty == types.float32:
            cval = ctypes.c_float(val)
            kernelargs.append(driver.KernelArg(cval))

        elif ty == types.boolean:
            cval = ctypes.c_uint8(int(val))
            kernelargs.append(driver.KernelArg(cval))

        elif ty == types.complex64:
            #kernelargs.append(ctypes.c_float(val.real))
            #kernelargs.append(ctypes.c_float(val.imag))
            raise NotImplementedError(ty, val)

        elif ty == types.complex128:
            #kernelargs.append(ctypes.c_double(val.real))
            #kernelargs.append(ctypes.c_double(val.imag))
            raise NotImplementedError(ty, val)

        else:
            raise NotImplementedError(ty, val)

    def check_for_invalid_access_type(self, access_type):
        if access_type not in self.valid_access_types:
            msg = ("[!] %s is not a valid access type. "
                  "Supported access types are [" % (access_type))
            for key in self.valid_access_types:
                msg += " %s |" % (key)

            msg = msg[:-1] + "]"
            if access_type != None: print(msg)
            return True
        else:
            return False


class JitDPPLKernel(DPPLKernelBase):
    def __init__(self, func, access_types):

        super(JitDPPLKernel, self).__init__()

        self.py_func = func
        self.definitions = {}
        self.access_types = access_types

        from .descriptor import dppl_target

        self.typingctx = dppl_target.typing_context

    def __call__(self, *args, **kwargs):
        assert not kwargs, "Keyword Arguments are not supported"
        if self.device_env is None:
            try:
                self.device_env = driver.runtime.get_current_device()
            except:
                _raise_no_device_found_error()

        kernel = self.specialize(*args)
        cfg = kernel.configure(self.device_env, self.global_size,
                               self.local_size)
        cfg(*args)

    def specialize(self, *args):
        argtypes = tuple([self.typingctx.resolve_argument_type(a)
                          for a in args])
        key_definitions = (self.device_env._env_ptr, argtypes)
        kernel = self.definitions.get(key_definitions)
        if kernel is None:
            kernel = compile_kernel(self.device_env, self.py_func, argtypes,
                                    self.access_types)
            self.definitions[key_definitions] = kernel
        return kernel
