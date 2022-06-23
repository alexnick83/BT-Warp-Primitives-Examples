import cupy
import numpy as np
import dace
from dace.sdfg import nodes
from dace.transformation.auto import auto_optimize as auto
from dace.transformation.dataflow import MapExpansion


N = dace.symbol('N')
sz = 100000000


@dace.program
def inner_product_python(A: dace.float64[N], B: dace.float64[N]):
    return np.add.reduce(A * B)


sdfg = inner_product_python.to_sdfg(simplify=True)
for _, arr in sdfg.arrays.items():
    if not arr.transient:
        arr.storage = dace.StorageType.GPU_Global
auto.auto_optimize(sdfg, dace.DeviceType.GPU)


A = np.ones((sz))
gA = cupy.asarray(A)
B = np.ones((sz))
gB = cupy.asarray(B)

res = sdfg(A=gA, B=gB, N=sz)
print(res)

sdfg2 = dace.SDFG('reduction')
sdfg2.add_array('A', shape=[N], dtype=dace.float64, storage=dace.StorageType.GPU_Global)
sdfg2.add_array('B', shape=[N], dtype=dace.float64, storage=dace.StorageType.GPU_Global)
sdfg2.add_array('__return', shape=[1], dtype=dace.float64, storage=dace.StorageType.GPU_Global)


def KernelCall(state):

    tasklet_code = f"""
    if (i + j == 0) {{
        out[0] = double(0);
    }}
    double sum = double(0);
    for (int id = i * 256 + j; id < N; id += blockDim.x * gridDim.x) {{
        sum += in1[id] * in2[id];
    }}
    for (int offset = warpSize/2; offset > 0; offset /= 2) {{
        sum += __shfl_down_sync(0xFFFFFFFF, sum, offset);
    }}
    if (j % warpSize == 0) {{
        atomicAdd(out, sum);
    }}
    """

    tasklet, me, mx = state.add_mapped_tasklet(
        name='callingKernel',
        map_ranges={'i': '0:min(int_ceil(N, 256), 2048)', 'j': '0:256'},
        inputs={'in1': dace.Memlet('A[0:N]'), 'in2': dace.Memlet('B[0:N]')},
        outputs={'out': dace.Memlet('__return[0]')},
        code=tasklet_code,
        language=dace.dtypes.Language.CPP,
        external_edges=True
    )
    out_conn = {'out': dace.pointer(dace.float64)}
    tasklet.out_connectors = out_conn

    me.map.schedule = dace.dtypes.ScheduleType.GPU_Device

callState = sdfg2.add_state()
KernelCall(callState)

sdfg2.apply_transformations_repeated(MapExpansion)

for n in callState.nodes():
    if isinstance(n, nodes.MapEntry) and "j" in n.map.params:
        n.map.schedule = dace.dtypes.ScheduleType.GPU_ThreadBlock
        
res2 = sdfg2(A=gA, B=gB, N=sz)
print(res2)

assert(np.allclose(res, res2))
