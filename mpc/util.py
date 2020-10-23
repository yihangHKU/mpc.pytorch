import torch
import numpy as np
from torch.autograd import Function, Variable
from torch.nn import Module
from torch.nn.parameter import Parameter

import operator

def jacobian(f, x, eps):
    if x.ndimension() == 2:
        assert x.size(0) == 1
        x = x.squeeze()

    e = Variable(torch.eye(len(x)).type_as(get_data_maybe(x)))
    J = []
    for i in range(len(x)):
        J.append((f(x + eps*e[i]) - f(x - eps*e[i]))/(2.*eps))
    J = torch.stack(J).transpose(0,1).transpose(1,2)
    return J


def expandParam(X, n_batch, nDim):
    if X.ndimension() in (0, nDim):
        return X, False
    elif X.ndimension() == nDim - 1:
        return X.unsqueeze(0).expand(*([n_batch] + list(X.size()))), True
    else:
        raise RuntimeError("Unexpected number of dimensions.")


def bdiag(d):
    assert d.ndimension() == 2
    nBatch, sz = d.size()
    dtype = d.type() if not isinstance(d, Variable) else d.data.type()
    D = torch.zeros(nBatch, sz, sz).type(dtype)
    I = torch.eye(sz).repeat(nBatch, 1, 1).type(dtype).byte()
    D[I] = d.view(-1)
    return D


def bger(x, y):
    return np.matmul(np.expand_dims(x, 2), np.expand_dims(y, 1))


def bmv(X, y):
    return np.matmul(X, np.expand_dims(y, 2)).squeeze(2)


def bquad(x, Q):
    return np.matmul(np.matmul(np.expand_dims(x, 1), Q), np.expand_dims(x, 2)).squeeze(1).squeeze(1)


def bdot(x, y):
    return torch.bmm(x.unsqueeze(1), y.unsqueeze(2)).squeeze(1).squeeze(1)


def eclamp(x, lower, upper):
    # In-place!!
    if type(lower) == type(x):
        assert x.shape == lower.shape

    if type(upper) == type(x):
        assert x.shape == upper.shape

    I = x < lower
    x[I] = lower[I] if not isinstance(lower, float) else lower

    I = x > upper
    x[I] = upper[I] if not isinstance(upper, float) else upper

    return x


def get_data_maybe(x):
    return x if not isinstance(x, Variable) else x.data


_seen_tables = []
def table_log(tag, d):
    # TODO: There's probably a better way to handle formatting here,
    # or a better way altogether to replace this quick hack.
    global _seen_tables

    def print_row(r):
        print('| ' + ' | '.join(r) + ' |')

    if tag not in _seen_tables:
        print_row(map(operator.itemgetter(0), d))
        _seen_tables.append(tag)

    s = []
    for di in d:
        assert len(di) in [2,3]
        if len(di) == 3:
            e, fmt = di[1:]
            s.append(fmt.format(e))
        else:
            e = di[1]
            s.append(str(e))
    print_row(s)


def get_traj(T, u, x_init, dynamics):
    from .mpc import QuadCost, LinDx # TODO: This is messy.

    if isinstance(dynamics, LinDx):
        F = dynamics.F
        f = dynamics.f
        if f is not None:
            assert f.shape == F.shape[:3]

    x = [x_init]
    for t in range(T):
        xt = x[t]
        ut = u[t]
        if t < T-1:
            # new_x = f(Variable(xt), Variable(ut)).data
            if isinstance(dynamics, LinDx):
                xut = np.concatenate((xt, ut), 1)
                new_x = bmv(F[t], xut)
                if f is not None:
                    new_x += f[t]
            else:
                new_x = dynamics(xt, ut)
            x.append(new_x)
    x = np.stack(x, axis=0)
    return x


def get_cost(T, u, cost, dynamics=None, x_init=None, x=None):
    from .mpc import QuadCost, LinDx # TODO: This is messy.

    assert x_init is not None or x is not None

    if isinstance(cost, QuadCost):
        C = cost.C
        c = cost.c

    if x is None:
        x = get_traj(T, u, x_init, dynamics)

    objs = []
    for t in range(T):
        xt = x[t]
        ut = u[t]
        xut = np.concatenate((xt, ut), 1)
        if isinstance(cost, QuadCost):
            # obj = 0.5*bquad(xut, C[t]) + bdot(xut, c[t]) + \
            #       0.5*bquad(torch.cat((dynamics.goal_state.repeat(1,1), dynamics.goal_ctrl.repeat(1,1)), dim=1), C[t])
            obj = 0.5 * bquad(xut - np.concatenate((np.expand_dims(dynamics.goal_state, 0), np.expand_dims(dynamics.goal_ctrl, 0)), axis=1), C[t])
        else:
            obj = cost(xut)
        objs.append(obj)
    # objs = np.stack(objs, dim=0)
    total_obj = np.sum(objs, axis=0)
    return total_obj


def detach_maybe(x):
    if x is None:
        return None
    return x if not x.requires_grad else x.detach()


def data_maybe(x):
    if x is None:
        return None
    return x.data
