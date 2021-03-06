from functools import reduce
import warnings
import tensorflow as tf
from . import kernels
from .tf_wraps import eye
from ._settings import settings

from .quadrature import mvhermgauss
from numpy import pi as nppi

int_type = settings.dtypes.int_type
float_type = settings.dtypes.float_type


class RBF(kernels.RBF):
    def eKdiag(self, X, Xcov=None):
        """
        Also known as phi_0.
        :param X:
        :return: N
        """
        return self.Kdiag(X)

    def eKxz(self, Z, Xmu, Xcov):
        """
        Also known as phi_1: <K_{x, Z}>_{q(x)}.
        :param Z: MxD inducing inputs
        :param Xmu: X mean (NxD)
        :param Xcov: NxDxD
        :return: NxM
        """
        # use only active dimensions
        Xcov = self._slice_cov(Xcov)
        Z, Xmu = self._slice(Z, Xmu)
        M = tf.shape(Z)[0]
        D = tf.shape(Xmu)[1]
        lengthscales = self.lengthscales if self.ARD else tf.zeros((D,), dtype=float_type) + self.lengthscales

        vec = tf.expand_dims(Xmu, 2) - tf.expand_dims(tf.transpose(Z), 0)  # NxDxM
        scalemat = tf.expand_dims(tf.diag(lengthscales ** 2.0), 0) + Xcov  # NxDxD
        smIvec = tf.matrix_solve(scalemat, vec) # NxDxM
        q = tf.reduce_sum(smIvec * vec, [1]) # NxM

        det = tf.matrix_determinant(
            tf.expand_dims(eye(D), 0) + tf.reshape(lengthscales ** -2.0, (1, 1, -1)) * Xcov
        )  # N
        return self.variance * tf.expand_dims(det ** -0.5, 1) * tf.exp(-0.5 * q)

    def exKxz(self, Z, Xmu, Xcov):
        """
        <x_t K_{x_{t-1}, Z}>_q_{x_{t-1:t}}
        :param Z: MxD inducing inputs
        :param Xmu: X mean (N+1xD)
        :param Xcov: 2x(N+1)xDxD
        :return: NxMxD
        """
        with tf.control_dependencies([
            tf.assert_equal(tf.shape(Xmu)[1], tf.constant(self.input_dim, dtype=int_type),
                            message="Currently cannot handle slicing in exKxz."),
            tf.assert_equal(tf.shape(Xmu), tf.shape(Xcov)[1:3], name="assert_Xmu_Xcov_shape")
        ]):
            Xmu = tf.identity(Xmu)

        M = tf.shape(Z)[0]
        N = tf.shape(Xmu)[0] - 1
        D = tf.shape(Xmu)[1]
        Xsigmb = tf.slice(Xcov, [0, 0, 0, 0], tf.stack([-1, N, -1, -1]))
        Xsigm = Xsigmb[0, :, :, :]  # NxDxD
        Xsigmc = Xsigmb[1, :, :, :]  # NxDxD
        Xmum = tf.slice(Xmu, [0, 0], tf.stack([N, -1]))
        Xmup = Xmu[1:, :]
        lengthscales = self.lengthscales if self.ARD else tf.zeros((D,), dtype=float_type) + self.lengthscales
        scalemat = tf.expand_dims(tf.diag(lengthscales ** 2.0), 0) + Xsigm  # NxDxD

        det = tf.matrix_determinant(
            tf.expand_dims(eye(tf.shape(Xmu)[1]), 0) + tf.reshape(lengthscales ** -2.0, (1, 1, -1)) * Xsigm
        )  # N

        vec = tf.expand_dims(tf.transpose(Z), 0) - tf.expand_dims(Xmum, 2)  # NxDxM
        smIvec = tf.matrix_solve(scalemat, vec)  # NxDxM
        q = tf.reduce_sum(smIvec * vec, [1])  # NxM

        addvec = tf.matmul(smIvec, Xsigmc, transpose_a=True) + tf.expand_dims(Xmup, 1)  # NxMxD

        return self.variance * addvec * tf.reshape(det ** -0.5, (N, 1, 1)) * tf.expand_dims(tf.exp(-0.5 * q), 2)

    def eKzxKxz(self, Z, Xmu, Xcov):
        """
        Also known as Phi_2.
        :param Z: MxD
        :param Xmu: X mean (NxD)
        :param Xcov: X covariance matrices (NxDxD)
        :return: NxMxM
        """
        # use only active dimensions
        Xcov = self._slice_cov(Xcov)
        Z, Xmu = self._slice(Z, Xmu)
        M = tf.shape(Z)[0]
        N = tf.shape(Xmu)[0]
        D = tf.shape(Xmu)[1]
        lengthscales = self.lengthscales if self.ARD else tf.zeros((D,), dtype=float_type) + self.lengthscales

        Kmms = tf.sqrt(self.K(Z, presliced=True)) / self.variance ** 0.5
        scalemat = tf.expand_dims(eye(D), 0) + 2 * Xcov * tf.reshape(lengthscales ** -2.0, [1, 1, -1])  # NxDxD
        det = tf.matrix_determinant(scalemat)

        mat = Xcov + 0.5 * tf.expand_dims(tf.diag(lengthscales ** 2.0), 0)  # NxDxD
        cm = tf.cholesky(mat)  # NxDxD
        vec = 0.5 * (tf.reshape(tf.transpose(Z), [1, D, 1, M]) +
                     tf.reshape(tf.transpose(Z), [1, D, M, 1])) - tf.reshape(Xmu, [N, D, 1, 1])  # NxDxMxM
        svec = tf.reshape(vec, (N, D, M * M))
        ssmI_z = tf.matrix_triangular_solve(cm, svec)  # NxDx(M*M)
        smI_z = tf.reshape(ssmI_z, (N, D, M, M)) # NxDxMxM
        fs = tf.reduce_sum(tf.square(smI_z), [1]) # NxMxM

        return self.variance ** 2.0 * tf.expand_dims(Kmms, 0) * tf.exp(-0.5 * fs) * tf.reshape(det ** -0.5, [N, 1, 1])


class Linear(kernels.Linear):
    def eKdiag(self, X, Xcov):
        if self.ARD:
            raise NotImplementedError
        # use only active dimensions
        X, _ = self._slice(X, None)
        Xcov = self._slice_cov(Xcov)
        return self.variance * (tf.reduce_sum(tf.square(X), 1) + tf.reduce_sum(tf.matrix_diag_part(Xcov), 1))

    def eKxz(self, Z, Xmu, Xcov):
        if self.ARD:
            raise NotImplementedError
        # use only active dimensions
        Z, Xmu = self._slice(Z, Xmu)
        return self.variance * tf.matmul(Xmu, tf.transpose(Z))

    def exKxz(self, Z, Xmu, Xcov):
        with tf.control_dependencies([
            tf.assert_equal(tf.shape(Xmu)[1], tf.constant(self.input_dim, int_type),
                            message="Currently cannot handle slicing in exKxz."),
            tf.assert_equal(tf.shape(Xmu), tf.shape(Xcov)[1:3], name="assert_Xmu_Xcov_shape")
        ]):
            Xmu = tf.identity(Xmu)

        N = tf.shape(Xmu)[0] - 1
        Xmum = Xmu[:-1, :]
        Xmup = Xmu[1:, :]
        op = tf.expand_dims(Xmum, 2) * tf.expand_dims(Xmup, 1) + Xcov[1, :-1, :, :]  # NxDxD
        return self.variance * tf.matmul(tf.tile(tf.expand_dims(Z, 0), (N, 1, 1)), op)

    def eKzxKxz(self, Z, Xmu, Xcov):
        """
        exKxz
        :param Z: MxD
        :param Xmu: NxD
        :param Xcov: NxDxD
        :return:
        """
        # use only active dimensions
        Xcov = self._slice_cov(Xcov)
        Z, Xmu = self._slice(Z, Xmu)
        N = tf.shape(Xmu)[0]
        mom2 = tf.expand_dims(Xmu, 1) * tf.expand_dims(Xmu, 2) + Xcov  # NxDxD
        eZ = tf.tile(tf.expand_dims(Z, 0), (N, 1, 1))  # NxMxD
        return self.variance ** 2.0 * tf.matmul(tf.matmul(eZ, mom2), eZ, transpose_b=True)


class Add(kernels.Add):
    """
    Add
    This version of Add will call the corresponding kernel expectations for each of the summed kernels. This will be
    much better for kernels with analytically calculated kernel expectations. If quadrature is to be used, it's probably
    better to do quadrature on the summed kernel function using `GPflow.kernels.Add` instead.
    """

    def __init__(self, kern_list):
        self.crossexp_funcs = {frozenset([Linear, RBF]): self.Linear_RBF_eKxzKzx}
        # self.crossexp_funcs = {}
        kernels.Add.__init__(self, kern_list)

    def eKdiag(self, X, Xcov):
        return reduce(tf.add, [k.eKdiag(X, Xcov) for k in self.kern_list])

    def eKxz(self, Z, Xmu, Xcov):
        return reduce(tf.add, [k.eKxz(Z, Xmu, Xcov) for k in self.kern_list])

    def exKxz(self, Z, Xmu, Xcov):
        return reduce(tf.add, [k.exKxz(Z, Xmu, Xcov) for k in self.kern_list])

    def eKzxKxz(self, Z, Xmu, Xcov):
        all_sum = reduce(tf.add, [k.eKzxKxz(Z, Xmu, Xcov) for k in self.kern_list])

        if self.on_separate_dimensions and Xcov.get_shape().ndims == 2:
            # If we're on separate dimensions and the covariances are diagonal, we don't need Cov[Kzx1Kxz2].
            crossmeans = []
            eKxzs = [k.eKxz(Z, Xmu, Xcov) for k in self.kern_list]
            for i, Ka in enumerate(eKxzs):
                for Kb in eKxzs[i + 1:]:
                    op = Ka[:, None, :] * Kb[:, :, None]
                    ct = tf.transpose(op, [0, 2, 1]) + op
                    crossmeans.append(ct)
            crossmean = reduce(tf.add, crossmeans)
            return all_sum + crossmean
        else:
            crossexps = []
            for i, ka in enumerate(self.kern_list):
                for kb in self.kern_list[i + 1:]:
                    try:
                        crossexp_func = self.crossexp_funcs[frozenset([type(ka), type(kb)])]
                        crossexp = crossexp_func(ka, kb, Z, Xmu, Xcov)
                    except (KeyError, NotImplementedError) as e:
                        print(str(e))
                        crossexp = self.quad_eKzx1Kxz2(ka, kb, Z, Xmu, Xcov)
                    crossexps.append(crossexp)
            return all_sum + reduce(tf.add, crossexps)

    def Linear_RBF_eKxzKzx(self, Ka, Kb, Z, Xmu, Xcov):
        Xcov = self._slice_cov(Xcov)
        Z, Xmu = self._slice(Z, Xmu)
        lin, rbf = (Ka, Kb) if type(Ka) is Linear else (Kb, Ka)
        assert type(lin) is Linear, "%s is not %s" % (str(type(lin)), str(Linear))
        assert type(rbf) is RBF, "%s is not %s" % (str(type(rbf)), str(RBF))
        if lin.ARD or type(lin.active_dims) is not slice or type(rbf.active_dims) is not slice:
            raise NotImplementedError("Active dims and/or Linear ARD not implemented. Switching to quadrature.")
        D = tf.shape(Xmu)[1]
        M = tf.shape(Z)[0]
        N = tf.shape(Xmu)[0]
        lengthscales = rbf.lengthscales if rbf.ARD else tf.zeros((D,), dtype=float_type) + rbf.lengthscales
        lengthscales2 = lengthscales ** 2.0

        const = rbf.variance * lin.variance * tf.reduce_prod(lengthscales)

        gaussmat = Xcov + tf.diag(lengthscales2)[None, :, :]  # NxDxD

        det = tf.matrix_determinant(gaussmat) ** -0.5  # N

        cgm = tf.cholesky(gaussmat)  # NxDxD
        tcgm = tf.tile(cgm[:, None, :, :], [1, M, 1, 1])
        vecmin = Z[None, :, :] - Xmu[:, None, :]  # NxMxD
        d = tf.matrix_triangular_solve(tcgm, vecmin[:, :, :, None])  # NxMxDx1
        exp = tf.exp(-0.5 * tf.reduce_sum(d ** 2.0, [2, 3]))  # NxM
        # exp = tf.Print(exp, [tf.shape(exp)])

        vecplus = (Z[None, :, :, None] / lengthscales2[None, None, :, None] +
                   tf.matrix_solve(Xcov, Xmu[:, :, None])[:, None, :, :])  # NxMxDx1
        mean = tf.cholesky_solve(tcgm,
                                 tf.matmul(tf.tile(Xcov[:, None, :, :], [1, M, 1, 1]), vecplus)
                                 )[:, :, :, 0] * lengthscales2[None, None, :]  # NxMxD
        a = tf.matmul(tf.tile(Z[None, :, :], [N, 1, 1]),
                            mean * exp[:, :, None] * det[:, None, None] * const, transpose_b=True)
        return a + tf.transpose(a, [0, 2, 1])

    def quad_eKzx1Kxz2(self, Ka, Kb, Z, Xmu, Xcov):
        # Quadrature for Cov[(Kzx1 - eKzx1)(kxz2 - eKxz2)]
        self._check_quadrature()
        warnings.warn("GPflow.ekernels.Add: Using numerical quadrature for kernel expectation cross terms.")
        Xmu, Z = self._slice(Xmu, Z)
        Xcov = self._slice_cov(Xcov)
        N, M, HpowD = tf.shape(Xmu)[0], tf.shape(Z)[0], self.num_gauss_hermite_points ** self.input_dim
        xn, wn = mvhermgauss(self.num_gauss_hermite_points, self.input_dim)

        # transform points based on Gaussian parameters
        cholXcov = tf.cholesky(Xcov)  # NxDxD
        Xt = tf.matmul(cholXcov, tf.tile(xn[None, :, :], (N, 1, 1)), transpose_b=True)  # NxDxH**D

        X = 2.0 ** 0.5 * Xt + tf.expand_dims(Xmu, 2)  # NxDxH**D
        Xr = tf.reshape(tf.transpose(X, [2, 0, 1]), (-1, self.input_dim))  # (H**D*N)xD

        cKa, cKb = [tf.reshape(
            k.K(tf.reshape(Xr, (-1, self.input_dim)), Z, presliced=False),
            (HpowD, N, M)
        ) - k.eKxz(Z, Xmu, Xcov)[None, :, :] for k in (Ka, Kb)]  # Centred Kxz
        eKa, eKb = Ka.eKxz(Z, Xmu, Xcov), Kb.eKxz(Z, Xmu, Xcov)

        wr = wn * nppi ** (-self.input_dim * 0.5)
        cc = tf.reduce_sum(cKa[:, :, None, :] * cKb[:, :, :, None] * wr[:, None, None, None], 0)
        cm = eKa[:, None, :] * eKb[:, :, None]
        return cc + tf.transpose(cc, [0, 2, 1]) + cm + tf.transpose(cm, [0, 2, 1])


class Prod(kernels.Prod):
    def eKdiag(self, Xmu, Xcov):
        if not self.on_separate_dimensions:
            raise NotImplementedError("Prod currently needs to be defined on separate dimensions.")  # pragma: no cover
        with tf.control_dependencies([
            tf.assert_equal(tf.rank(Xcov), 2,
                            message="Prod currently only supports diagonal Xcov.", name="assert_Xcov_diag"),
        ]):
            return reduce(tf.multiply, [k.eKdiag(Xmu, Xcov) for k in self.kern_list])

    def eKxz(self, Z, Xmu, Xcov):
        if not self.on_separate_dimensions:
            raise NotImplementedError("Prod currently needs to be defined on separate dimensions.")  # pragma: no cover
        with tf.control_dependencies([
            tf.assert_equal(tf.rank(Xcov), 2,
                            message="Prod currently only supports diagonal Xcov.", name="assert_Xcov_diag"),
        ]):
            return reduce(tf.multiply, [k.eKxz(Z, Xmu, Xcov) for k in self.kern_list])

    def eKzxKxz(self, Z, Xmu, Xcov):
        if not self.on_separate_dimensions:
            raise NotImplementedError("Prod currently needs to be defined on separate dimensions.")  # pragma: no cover
        with tf.control_dependencies([
            tf.assert_equal(tf.rank(Xcov), 2,
                            message="Prod currently only supports diagonal Xcov.", name="assert_Xcov_diag"),
        ]):
            return reduce(tf.multiply, [k.eKzxKxz(Z, Xmu, Xcov) for k in self.kern_list])
