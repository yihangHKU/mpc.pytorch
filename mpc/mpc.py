import torch
from torch.autograd import Function, Variable
from torch.nn import Module
from torch.nn.parameter import Parameter

import numpy as np
import numpy.random as npr
import time

from collections import namedtuple

from enum import Enum

import sys

from . import util
from .pnqp import pnqp
from .lqr_step import LQRStep
from .dynamics import CtrlPassthroughDynamics

Qfields = ('C', 'c')
Dfields = ('F', 'f')
QuadCost = namedtuple('QuadCost', Qfields, defaults=(None,) * len(Qfields))
LinDx = namedtuple('LinDx', Dfields, defaults=(None,) * len(Dfields))

# https://stackoverflow.com/questions/11351032
# QuadCost.__new__.__defaults__ = (None,) * len(QuadCost._fields)
# LinDx.__new__.__defaults__ = (None,) * len(LinDx._fields)


class GradMethods(Enum):
    AUTO_DIFF = 1
    FINITE_DIFF = 2
    ANALYTIC = 3
    ANALYTIC_CHECK = 4


class SlewRateCost(Module):
    """Hacky way of adding the slew rate penalty to costs."""
    # TODO: It would be cleaner to update this to just use the slew
    # rate penalty instead of # slew_C
    def __init__(self, cost, slew_C, n_state, n_ctrl):
        super().__init__()
        self.cost = cost
        self.slew_C = slew_C
        self.n_state = n_state
        self.n_ctrl = n_ctrl

    def forward(self, tau):
        true_tau = tau[:, self.n_ctrl:]
        true_cost = self.cost(true_tau)
        # The slew constraints are time-invariant.
        slew_cost = 0.5 * util.bquad(tau, self.slew_C[0])
        return true_cost + slew_cost

    def grad_input(self, x, u):
        raise NotImplementedError("Implement grad_input")


class MPC(Module):
    """A differentiable box-constrained iLQR solver.

    This provides a differentiable solver for the following box-constrained
    control problem with a quadratic cost (defined by C and c) and
    non-linear dynamics (defined by f):

        min_{tau={x,u}} sum_t 0.5 tau_t^T C_t tau_t + c_t^T tau_t
                        s.t. x_{t+1} = f(x_t, u_t)
                            x_0 = x_init
                            u_lower <= u <= u_upper

    This implements the Control-Limited Differential Dynamic Programming
    paper with a first-order approximation to the non-linear dynamics:
    https://homes.cs.washington.edu/~todorov/papers/TassaICRA14.pdf

    Some of the notation here is from Sergey Levine's notes:
    http://rll.berkeley.edu/deeprlcourse/f17docs/lecture_8_model_based_planning.pdf

    Required Args:
        n_state, n_ctrl, T

    Optional Args:
        u_lower, u_upper: The lower- and upper-bounds on the controls.
            These can either be floats or shaped as [T, n_batch, n_ctrl]
        u_init: The initial control sequence, useful for warm-starting:
            [T, n_batch, n_ctrl]
        lqr_iter: The number of LQR iterations to perform.
        grad_method: The method to compute the Jacobian of the dynamics.
            GradMethods.ANALYTIC: Use a manually-defined Jacobian.
                + Fast and accurate, use this if possible
            GradMethods.AUTO_DIFF: Use PyTorch's autograd.
                + Slow
            GradMethods.FINITE_DIFF: Use naive finite differences
                + Inaccurate
        delta_u (float): The amount each component of the controls
            is allowed to change in each LQR iteration.
        verbose (int):
            -1: No output or warnings
             0: Warnings
            1+: Detailed iteration info
        eps: Termination threshold, on the norm of the full control
             step (without line search)
        back_eps: `eps` value to use in the backwards pass.
        n_batch: May be necessary for now if it can't be inferred.
                 TODO: Infer, potentially remove this.
        linesearch_decay (float): Multiplicative decay factor for the
            line search.
        max_linesearch_iter (int): Can be used to disable the line search
            if 1 is used for some problems the line search can
            be harmful.
        exit_unconverged: Assert False if a fixed point is not reached.
        detach_unconverged: Detach examples from the graph that do
            not hit a fixed point so they are not differentiated through.
        backprop: Allow the solver to be differentiated through.
        slew_rate_penalty (float): Penalty term applied to
            ||u_t - u_{t+1}||_2^2 in the objective.
        prev_ctrl: The previous nominal control sequence to initialize
            the solver with.
        not_improved_lim: The number of iterations to allow that don't
            improve the objective before returning early.
        best_cost_eps: Absolute threshold for the best cost
            to be updated.
    """

    def __init__(
            self, n_state, n_ctrl, T,
            u_lower=None, u_upper=None,
            u_zero_I=None,
            u_init=None,
            lqr_iter=10,
            grad_method=GradMethods.ANALYTIC,
            delta_u=None,
            verbose=0,
            eps=1e-7,
            back_eps=1e-7,
            n_batch=None,
            linesearch_decay=0.2,
            max_linesearch_iter=10,
            exit_unconverged=True,
            detach_unconverged=True,
            backprop=True,
            slew_rate_penalty=None,
            prev_ctrl=None,
            not_improved_lim=3,
            best_cost_eps=1e-3
    ):
        super().__init__()

        assert (u_lower is None) == (u_upper is None)
        assert max_linesearch_iter > 0

        self.n_state = n_state
        self.n_ctrl = n_ctrl
        self.T = T
        self.u_lower = u_lower
        self.u_upper = u_upper

        # if not isinstance(u_lower, float):
        #     self.u_lower = util.detach_maybe(self.u_lower)
        #
        # if not isinstance(u_upper, float):
        #     self.u_upper = util.detach_maybe(self.u_upper)

        # self.u_zero_I = util.detach_maybe(u_zero_I)
        # self.u_init = util.detach_maybe(u_init)
        self.u_zero_I = u_zero_I
        self.u_init = u_init
        self.lqr_iter = lqr_iter
        self.grad_method = grad_method
        self.delta_u = delta_u
        self.verbose = verbose
        self.eps = eps
        self.back_eps = back_eps
        self.n_batch = n_batch
        self.linesearch_decay = linesearch_decay
        self.max_linesearch_iter = max_linesearch_iter
        self.exit_unconverged = exit_unconverged
        self.detach_unconverged = detach_unconverged
        self.backprop = backprop
        self.not_improved_lim = not_improved_lim
        self.best_cost_eps = best_cost_eps

        self.slew_rate_penalty = slew_rate_penalty
        self.prev_ctrl = prev_ctrl


    # @profile
    def forward(self, x_init, cost, dx):
        # QuadCost.C: [T, n_batch, n_tau, n_tau]
        # QuadCost.c: [T, n_batch, n_tau]
        assert isinstance(cost, QuadCost) or \
            isinstance(cost, Module) or isinstance(cost, Function)
        assert isinstance(dx, LinDx) or \
            isinstance(dx, Module) or isinstance(dx, Function)

        # TODO: Clean up inferences, expansions, and assumptions made here.
        if self.n_batch is not None:
            n_batch = self.n_batch
        elif isinstance(cost, QuadCost) and cost.C.ndim == 4:
            n_batch = cost.C.shape[1]
        else:
            print('MPC Error: Could not infer batch size, pass in as n_batch')
            sys.exit(-1)


        # if c.ndimension() == 2:
        #     c = c.unsqueeze(1).expand(self.T, n_batch, -1)

        if isinstance(cost, QuadCost):
            C, c = cost
            if C.ndim == 2:
                # Add the time and batch dimensions.
                C = np.tile(C, (self.T, n_batch, 1, 1))
            elif C.ndim == 3:
                # Add the batch dimension.
                C = np.tile(np.expand_dims(C, 1), (1, n_batch, 1, 1))

            if c.ndim == 1:
                # Add the time and batch dimensions.
                c = np.tile(c, (self.T, n_batch, 1))
            elif c.ndim == 2:
                # Add the batch dimension.
                c = np.tile(np.expand_dims(c, 1), (1, n_batch, 1))

            if C.ndim != 4 or c.ndim != 3:
                print('MPC Error: Unexpected QuadCost shape.')
                sys.exit(-1)
            cost = QuadCost(C, c)

        assert x_init.ndim == 2 and x_init.shape[0] == n_batch

        if self.u_init is None:
            u = np.zeros((self.T, n_batch, self.n_ctrl), dtype='single')
        else:
            u = self.u_init
            if u.ndim == 2:
                u = np.tile(np.expand_dims(u, 1), (1, n_batch, 1))

        if self.verbose > 0:
            print('Initial mean(cost): {:.4e}'.format(
                np.mean(util.get_cost(
                    self.T, u, cost, dx, x_init=x_init
                ))
            ))

        best = None

        n_not_improved = 0
        for i in range(self.lqr_iter):
            u = u
            # Linearize the dynamics around the current trajectory.
            # time3 = time.time()
            # print('begin get traj')
            x = util.get_traj(self.T, u, x_init=x_init, dynamics=dx)
            # print('end get traj')
            # time4 = time.time()
            # print('get trajectory time:', time4 - time3)
            if isinstance(dx, LinDx):
                F, f = dx.F, dx.f
            else:
                # start = time.time()
                F, f = self.linearize_dynamics(
                    x, u, dx, diff=False)
                # end = time.time()
                # print('dynamics linearize:',end-start)
            if isinstance(cost, QuadCost):
                C, c = cost.C, cost.c
            else:
                C, c, _ = self.approximate_cost(
                    x, u, cost, diff=False)

            x, u, _lqr = self.solve_lqr_subproblem(
                x_init, C, c, F, f, cost, dx, x, u, self.verbose)
            # print(u)
            back_out, for_out = _lqr.back_out, _lqr.for_out
            n_not_improved += 1
            assert x.ndim == 3
            assert u.ndim == 3

            if best is None:
                best = {
                    'x': list(np.split(x, indices_or_sections=1, axis=1)),
                    'u': list(np.split(u, indices_or_sections=1, axis=1)),
                    'costs': for_out.costs,
                    # 'costsxx': for_out.costsxx,
                    # 'costsuu': for_out.costsuu,
                    # 'costsx': for_out.costsx,
                    # 'costsu': for_out.costsu,
                    # 'objsxx': for_out.objsxx,
                    # 'objsuu': for_out.objsuu,
                    # 'objsx': for_out.objsx,
                    # 'objsu': for_out.objsu,
                    'full_du_norm': for_out.full_du_norm,
                }
            else:
                for j in range(n_batch):
                    if for_out.costs[j] <= best['costs'][j] - self.best_cost_eps:
                        n_not_improved = 0
                        best['x'][j] = np.expand_dims(x[:,j], 1)
                        best['u'][j] = np.expand_dims(u[:,j], 1)
                        best['costs'][j] = for_out.costs[j]
                        # best['costsxx'][j] = for_out.costsxx[j]
                        # best['costsuu'][j] = for_out.costsuu[j]
                        # best['costsx'][j] = for_out.costsx[j]
                        # best['costsu'][j] = for_out.costsu[j]
                        # best['objsxx'][:,j] = for_out.objsxx[:,j]
                        # best['objsuu'][:,j] = for_out.objsuu[:,j]
                        # best['objsx'][:,j] = for_out.objsx[:,j]
                        # best['objsu'][:,j] = for_out.objsu[:,j]
                        best['full_du_norm'][j] = for_out.full_du_norm[j]

            if self.verbose > 0:
                util.table_log('lqr', (
                    ('iter', i),
                    ('mean(cost)', np.mean(best['costs']).item(), '{:.4e}'),
                    ('mean(costxx)', np.mean(best['costsxx']).item(), '{:.4e}'),
                    ('mean(costuu)', np.mean(best['costsuu']).item(), '{:.4e}'),
                    # ('mean(costx)', np.mean(best['costsx']).item(), '{:.4e}'),
                    # ('mean(costu)', np.mean(best['costsu']).item(), '{:.4e}'),
                    ('mean(objsxx[0])', np.mean(best['objsxx'][0], ).item(), '{:.4e}'),
                    ('mean(objsuu[0])', np.mean(best['objsuu'][0], ).item(), '{:.4e}'),
                    ('mean(objsxx[1])', np.mean(best['objsxx'][1], ).item(), '{:.4e}'),
                    ('mean(objsuu[1])', np.mean(best['objsuu'][1], ).item(), '{:.4e}'),
                    ('mean(objsxx[2])', np.mean(best['objsxx'][2], ).item(), '{:.4e}'),
                    ('mean(objsuu[2])', np.mean(best['objsuu'][2], ).item(), '{:.4e}'),
                    ('mean(objsxx[3])', np.mean(best['objsxx'][3], ).item(), '{:.4e}'),
                    ('mean(objsuu[3])', np.mean(best['objsuu'][3], ).item(), '{:.4e}'),
                    # ('mean(objsxx[4])', np.mean(best['objsxx'][4], ).item(), '{:.4e}'),
                    # ('mean(objsuu[4])', np.mean(best['objsuu'][4], ).item(), '{:.4e}'),
                    # ('||full_du||_max', max(for_out.full_du_norm).item(), '{:.2e}'),
                    # ('||alpha_du||_max', max(for_out.alpha_du_norm), '{:.2e}'),
                    # TODO: alphas, total_qp_iters here is for the current
                    # iterate, not the best
                    # ('mean(alphas)', for_out.mean_alphas.item(), '{:.2e}'),
                    # ('total_qp_iters', back_out.n_total_qp_iter),
                ))

            if max(for_out.full_du_norm) < self.eps or \
               n_not_improved > self.not_improved_lim:
                break


        x = np.concatenate(best['x'], axis=1)
        u = np.concatenate(best['u'], axis=1)
        full_du_norm = best['full_du_norm']

        # if isinstance(dx, LinDx):
        #     F, f = dx.F, dx.f
        # else:
        #     time1 = time.time()
        #     F, f = self.linearize_dynamics(x, u, dx, diff=True)
        #     time2 = time.time()
        #     print('dynamics linearize2:', time2 - time1)
        #
        # if isinstance(cost, QuadCost):
        #     C, c = cost.C, cost.c
        # else:
        #     C, c, _ = self.approximate_cost(x, u, cost, diff=True)
        #
        # x, u, _ = self.solve_lqr_subproblem(
        #     x_init, C, c, F, f, cost, dx, x, u, no_op_forward=True)

        if self.detach_unconverged:
            if max(best['full_du_norm']) > self.eps:
                if self.exit_unconverged:
                    assert False

                if self.verbose >= 0:
                    print("LQR Warning: All examples did not converge to a fixed point.")
                    print("Detaching and *not* backpropping through the bad examples.")

                I = for_out.full_du_norm < self.eps
                Ix = Variable(I.unsqueeze(0).unsqueeze(2).expand_as(x)).type_as(x.data)
                Iu = Variable(I.unsqueeze(0).unsqueeze(2).expand_as(u)).type_as(u.data)
                x = x*Ix + x.clone().detach()*(1.-Ix)
                u = u*Iu + u.clone().detach()*(1.-Iu)

        costs = best['costs']
        return (x, u, costs)

    def solve_lqr_subproblem(self, x_init, C, c, F, f, cost, dynamics, x, u, verbose,
                             no_op_forward=False):
        if self.slew_rate_penalty is None or isinstance(cost, Module):
            _lqr = LQRStep(
                n_state=self.n_state,
                n_ctrl=self.n_ctrl,
                T=self.T,
                verbose=verbose,
                u_lower=self.u_lower,
                u_upper=self.u_upper,
                u_zero_I=self.u_zero_I,
                true_cost=cost,
                true_dynamics=dynamics,
                delta_u=self.delta_u,
                linesearch_decay=self.linesearch_decay,
                max_linesearch_iter=self.max_linesearch_iter,
                delta_space=True,
                current_x=x,
                current_u=u,
                back_eps=self.back_eps,
                no_op_forward=no_op_forward,
            )
            e = np.array([])
            x, u = _lqr(x_init, C, c, F, f if f is not None else e)
            return x, u, _lqr
        else:
            nsc = self.n_state + self.n_ctrl
            _n_state = nsc
            _nsc = _n_state + self.n_ctrl
            n_batch = C.shape[1]
            _C = np.zeros((self.T, n_batch, _nsc, _nsc), dtype='single')
            half_gamI = np.expand_dims(np.expand_dims(self.slew_rate_penalty * np.eye(
                self.n_ctrl), 0), 0).repeat(self.T, 0).repeat(n_batch, 1)
            _C[:,:,:self.n_ctrl,:self.n_ctrl] = half_gamI
            _C[:,:,-self.n_ctrl:,:self.n_ctrl] = -half_gamI
            _C[:,:,:self.n_ctrl,-self.n_ctrl:] = -half_gamI
            _C[:,:,-self.n_ctrl:,-self.n_ctrl:] = half_gamI
            slew_C = _C.copy()
            _C = _C + torch.nn.ZeroPad2d((self.n_ctrl, 0, self.n_ctrl, 0))(C)

            _c = torch.cat((
                torch.zeros(self.T, n_batch, self.n_ctrl).type_as(c),c), 2)

            _F0 = torch.cat((
                torch.zeros(self.n_ctrl, self.n_state+self.n_ctrl),
                torch.eye(self.n_ctrl),
            ), 1).type_as(F).unsqueeze(0).unsqueeze(0).repeat(
                self.T-1, n_batch, 1, 1
            )
            _F1 = torch.cat((
                torch.zeros(
                    self.T-1, n_batch, self.n_state, self.n_ctrl
                ).type_as(F),F), 3)
            _F = torch.cat((_F0, _F1), 2)

            if f is not None:
                _f = torch.cat((
                    torch.zeros(self.T-1, n_batch, self.n_ctrl).type_as(f),f), 2)
            else:
                _f = Variable(torch.Tensor())

            u_data = util.detach_maybe(u)
            if self.prev_ctrl is not None:
                prev_u = self.prev_ctrl
                if prev_u.ndimension() == 1:
                    prev_u = prev_u.unsqueeze(0)
                if prev_u.ndimension() == 2:
                    prev_u = prev_u.unsqueeze(0)
                prev_u = prev_u.data
            else:
                prev_u = torch.zeros(1, n_batch, self.n_ctrl).type_as(u)
            utm1s = torch.cat((prev_u, u_data[:-1])).clone()
            _x = torch.cat((utm1s, x), 2)

            _x_init = torch.cat((Variable(prev_u[0]), x_init), 1)

            if not isinstance(dynamics, LinDx):
                _dynamics = CtrlPassthroughDynamics(dynamics)
            else:
                _dynamics = None

            if isinstance(cost, QuadCost):
                _true_cost = QuadCost(_C, _c)
            else:
                _true_cost = SlewRateCost(
                    cost, slew_C, self.n_state, self.n_ctrl
                )

            _lqr = LQRStep(
                n_state=_n_state,
                n_ctrl=self.n_ctrl,
                T=self.T,
                u_lower=self.u_lower,
                u_upper=self.u_upper,
                u_zero_I=self.u_zero_I,
                true_cost=_true_cost,
                true_dynamics=_dynamics,
                delta_u=self.delta_u,
                linesearch_decay=self.linesearch_decay,
                max_linesearch_iter=self.max_linesearch_iter,
                delta_space=True,
                current_x=_x,
                current_u=u,
                back_eps=self.back_eps,
                no_op_forward=no_op_forward,
            )
            x, u = _lqr(_x_init, _C, _c, _F, _f)
            x = x[:,:,self.n_ctrl:]

            return x, u, _lqr

    def approximate_cost(self, x, u, Cf, diff=True):
        with torch.enable_grad():
            tau = torch.cat((x, u), dim=2).data
            tau = Variable(tau, requires_grad=True)
            if self.slew_rate_penalty is not None:
                print("""
MPC Error: Using a non-convex cost with a slew rate penalty is not yet implemented.
The current implementation does not correctly do a line search.
More details: https://github.com/locuslab/mpc.pytorch/issues/12
""")
                sys.exit(-1)
                differences = tau[1:, :, -self.n_ctrl:] - tau[:-1, :, -self.n_ctrl:]
                slew_penalty = (self.slew_rate_penalty * differences.pow(2)).sum(-1)
            costs = list()
            hessians = list()
            grads = list()
            for t in range(self.T):
                tau_t = tau[t]
                if self.slew_rate_penalty is not None:
                    cost = Cf(tau_t) + (slew_penalty[t-1] if t > 0 else 0)
                else:
                    cost = Cf(tau_t)

                grad = torch.autograd.grad(cost.sum(), tau_t,
                                           retain_graph=True)[0]
                hessian = list()
                for v_i in range(tau.shape[2]):
                    hessian.append(
                        torch.autograd.grad(grad[:, v_i].sum(), tau_t,
                                            retain_graph=True)[0]
                    )
                hessian = torch.stack(hessian, dim=-1)
                costs.append(cost)
                grads.append(grad - util.bmv(hessian, tau_t))
                hessians.append(hessian)
            costs = torch.stack(costs, dim=0)
            grads = torch.stack(grads, dim=0)
            hessians = torch.stack(hessians, dim=0)
            if not diff:
                return hessians.data, grads.data, costs.data
            return hessians, grads, costs

    # @profile
    def linearize_dynamics(self, x, u, dynamics, diff):
        # TODO: Cleanup variable usage.
        n_batch = x[0].shape[0]

        if self.grad_method == GradMethods.ANALYTIC:
            _u = u[:-1].reshape(-1, self.n_ctrl)
            _x = x[:-1].reshape(-1, self.n_state)

            # This inefficiently calls dynamics again, but is worth it because
            # we can efficiently compute grad_input for every time step at once.
            _new_x = dynamics(_x, _u)
            # This check is a little expensive and should only be done if
            # modifying this code.
            # assert torch.abs(_new_x.data - x[1:].view(-1, self.n_state)).max() <= 1e-6

            # if not diff:
            #     _new_x = _new_x.data
            #     _x = _x.data
            #     _u = _u.data
            R, S = dynamics.grad_input(_x, _u)

            f = _new_x - util.bmv(R, _x) - util.bmv(S, _u)
            f = f.reshape(self.T-1, n_batch, self.n_state)

            R = R.reshape(self.T-1, n_batch, self.n_state, self.n_state)
            S = S.reshape(self.T-1, n_batch, self.n_state, self.n_ctrl)
            F = np.concatenate((R, S), 3)

            return F, f
        else:
            # TODO: This is inefficient and confusing.
            x_init = x[0]
            x = [x_init]
            F, f = [], []
            for t in range(self.T):
                if t < self.T-1:
                    xt = Variable(x[t], requires_grad=True)
                    ut = Variable(u[t], requires_grad=True)
                    xut = torch.cat((xt, ut), 1)
                    new_x = dynamics(xt, ut)

                    # Linear dynamics approximation.
                    if self.grad_method in [GradMethods.AUTO_DIFF,
                                             GradMethods.ANALYTIC_CHECK]:
                        Rt, St = [], []
                        for j in range(self.n_state):
                            Rj, Sj = torch.autograd.grad(
                                new_x[:,j].sum(), [xt, ut],
                                retain_graph=True)
                            if not diff:
                                Rj, Sj = Rj.data, Sj.data
                            Rt.append(Rj)
                            St.append(Sj)
                        Rt = torch.stack(Rt, dim=1)
                        St = torch.stack(St, dim=1)

                        if self.grad_method == GradMethods.ANALYTIC_CHECK:
                            assert False # Not updated
                            Rt_autograd, St_autograd = Rt, St
                            Rt, St = dynamics.grad_input(xt, ut)
                            eps = 1e-8
                            if torch.max(torch.abs(Rt-Rt_autograd)).data[0] > eps or \
                            torch.max(torch.abs(St-St_autograd)).data[0] > eps:
                                print('''
        nmpc.ANALYTIC_CHECK error: The analytic derivative of the dynamics function may be off.
                                ''')
                            else:
                                print('''
        nmpc.ANALYTIC_CHECK: The analytic derivative of the dynamics function seems correct.
        Re-run with GradMethods.ANALYTIC to continue.
                                ''')
                            sys.exit(0)
                    elif self.grad_method == GradMethods.FINITE_DIFF:
                        Rt, St = [], []
                        for i in range(n_batch):
                            Ri = util.jacobian(
                                lambda s: dynamics(s, ut[i]), xt[i], 1e-3
                            )
                            Si = util.jacobian(
                                lambda a : dynamics(xt[i], a), ut[i], 1e-3
                            )
                            if not diff:
                                Ri, Si = Ri.data, Si.data
                            Rt.append(Ri)
                            St.append(Si)
                        Rt = torch.stack(Rt)
                        St = torch.stack(St)
                        Rt = Rt.squeeze(0)
                        St = St.squeeze(0)
                    else:
                        assert False

                    Ft = torch.cat((Rt, St), 2)
                    F.append(Ft)

                    if not diff:
                        xt, ut, new_x = xt.data, ut.data, new_x.data
                    ft = new_x - util.bmv(Rt, xt) - util.bmv(St, ut)
                    f.append(ft)

                if t < self.T-1:
                    x.append(util.detach_maybe(new_x))

            F = torch.stack(F, 0)
            f = torch.stack(f, 0)
            if not diff:
                F, f = list(map(Variable, [F, f]))
            return F, f
