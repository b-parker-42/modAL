import numpy as np

from typing import Callable, Optional, Tuple, List, Any

from sklearn.base import BaseEstimator
from sklearn.metrics import accuracy_score

from sklearn.utils import check_X_y
from modAL.models.base import BaseLearner, BaseCommittee
from modAL.utils.validation import check_class_labels, check_class_proba
from modAL.utils.data import modALinput, retrieve_rows, data_vstack
from modAL.uncertainty import uncertainty_sampling
from modAL.disagreement import vote_entropy_sampling, max_std_sampling
from modAL.acquisition import max_EI

from skorch.utils import to_numpy


"""
Classes for active learning algorithms
--------------------------------------
"""


class ActiveLearner(BaseLearner):
    """
    This class is an model of a general classic (machine learning) active learning algorithm.

    Args:
        estimator: The estimator to be used in the active learning loop.
        query_strategy: Function providing the query strategy for the active learning loop,
            for instance, modAL.uncertainty.uncertainty_sampling.
        X_training: Initial training samples, if available.
        y_training: Initial training labels corresponding to initial training samples.
        bootstrap_init: If initial training data is available, bootstrapping can be done during the first training.
            Useful when building Committee models with bagging.
        on_transformed: Whether to transform samples with the pipeline defined by the estimator
            when applying the query strategy.
        **fit_kwargs: keyword arguments.

    Attributes:
        estimator: The estimator to be used in the active learning loop.
        query_strategy: Function providing the query strategy for the active learning loop.
        X_training: If the model hasn't been fitted yet it is None, otherwise it contains the samples
            which the model has been trained on. If provided, the method fit() of estimator is called during __init__()
        y_training: The labels corresponding to X_training.

    Examples:

        >>> from sklearn.datasets import load_iris
        >>> from sklearn.ensemble import RandomForestClassifier
        >>> from modAL.models import ActiveLearner
        >>> iris = load_iris()
        >>> # give initial training examples
        >>> X_training = iris['data'][[0, 50, 100]]
        >>> y_training = iris['target'][[0, 50, 100]]
        >>>
        >>> # initialize active learner
        >>> learner = ActiveLearner(
        ...     estimator=RandomForestClassifier(),
        ...     X_training=X_training, y_training=y_training
        ... )
        >>>
        >>> # querying for labels
        >>> query_idx, query_sample = learner.query(iris['data'])
        >>>
        >>> # ...obtaining new labels from the Oracle...
        >>>
        >>> # teaching newly labelled examples
        >>> learner.teach(
        ...     X=iris['data'][query_idx].reshape(1, -1),
        ...     y=iris['target'][query_idx].reshape(1, )
        ... )
    """

    def __init__(self,
                 estimator: BaseEstimator,
                 query_strategy: Callable = uncertainty_sampling,
                 X_training: Optional[modALinput] = None,
                 y_training: Optional[modALinput] = None,
                 bootstrap_init: bool = False,
                 on_transformed: bool = False,
                 **fit_kwargs
                 ) -> None:
        super().__init__(estimator, query_strategy, on_transformed, **fit_kwargs)
        
        self.X_training = X_training
        self.y_training = y_training

        if X_training is not None:
            self._fit_to_known(bootstrap=bootstrap_init, **fit_kwargs)

    def _add_training_data(self, X: modALinput, y: modALinput) -> None:
        """
        Adds the new data and label to the known data, but does not retrain the model.

        Args:
            X: The new samples for which the labels are supplied by the expert.
            y: Labels corresponding to the new instances in X.

        Note:
            If the classifier has been fitted, the features in X have to agree with the training samples which the
            classifier has seen.
        """
        check_X_y(X, y, accept_sparse=True, ensure_2d=False, allow_nd=True, multi_output=True, dtype=None,
                force_all_finite=self.force_all_finite)

        if self.X_training is None:
            self.X_training = X
            self.y_training = y
        else:
            try:
                self.X_training = data_vstack((self.X_training, X))
                self.y_training = data_vstack((self.y_training, y))
            except ValueError:
                raise ValueError('the dimensions of the new training data and label must'
                                 'agree with the training data and labels provided so far')

    def _fit_to_known(self, bootstrap: bool = False, **fit_kwargs) -> 'BaseLearner':
        """
        Fits self.estimator to the training data and labels provided to it so far.

        Args:
            bootstrap: If True, the method trains the model on a set bootstrapped from the known training instances.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.

        Returns:
            self
        """
        if not bootstrap:
            self.estimator.fit(self.X_training, self.y_training, **fit_kwargs)
        else:
            n_instances = self.X_training.shape[0]
            bootstrap_idx = np.random.choice(range(n_instances), n_instances, replace=True)
            self.estimator.fit(self.X_training[bootstrap_idx], self.y_training[bootstrap_idx], **fit_kwargs)

        return self    
    
    def fit(self, X: modALinput, y: modALinput, bootstrap: bool = False, **fit_kwargs) -> 'BaseLearner':
        """
        Interface for the fit method of the predictor. Fits the predictor to the supplied data, then stores it
        internally for the active learning loop.

        Args:
            X: The samples to be fitted.
            y: The corresponding labels.
            bootstrap: If true, trains the estimator on a set bootstrapped from X.
                Useful for building Committee models with bagging.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.

        Note:
            When using scikit-learn estimators, calling this method will make the ActiveLearner forget all training data
            it has seen!

        Returns:
            self
        """            
        check_X_y(X, y, accept_sparse=True, ensure_2d=False, allow_nd=True, multi_output=True, dtype=None,
                    force_all_finite=self.force_all_finite)
        self.X_training, self.y_training = X, y
        return self._fit_to_known(bootstrap=bootstrap, **fit_kwargs)

    def score(self, X: modALinput, y: modALinput, **score_kwargs) -> Any:
        """
        Interface for the score method of the predictor.

        Args:
            X: The samples for which prediction accuracy is to be calculated.
            y: Ground truth labels for X.
            **score_kwargs: Keyword arguments to be passed to the .score() method of the predictor.

        Returns:
            The score of the predictor.
        """
        return self.estimator.score(X, y, **score_kwargs)

    def teach(self, X: modALinput, y: modALinput, bootstrap: bool = False, only_new: bool = False, **fit_kwargs) -> None:
        """
        Adds X and y to the known training data and retrains the predictor with the augmented dataset.

        Args:
            X: The new samples for which the labels are supplied by the expert.
            y: Labels corresponding to the new instances in X.
            bootstrap: If True, training is done on a bootstrapped dataset. Useful for building Committee models
                with bagging.
            only_new: If True, the model is retrained using only X and y, ignoring the previously provided examples.
                Useful when working with models where the .fit() method doesn't retrain the model from scratch (e. g. in
                tensorflow or keras).
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """
        if not only_new:
            self._add_training_data(X, y)
            self._fit_to_known(bootstrap=bootstrap, **fit_kwargs)
        else:
            check_X_y(X, y, accept_sparse=True, ensure_2d=False, allow_nd=True, multi_output=True, dtype=None,
                force_all_finite=self.force_all_finite)
            self._fit_on_new(X, y, bootstrap=bootstrap, **fit_kwargs)

class DeepActiveLearner(BaseLearner):
    """
    This class is an model of a general deep active learning algorithm.
    Differences to the classical ActiveLearner are:
        - Data is no member variable of the DeepActiveLearner class
        - Misses the initial add/train data methods, therefore always trains on new data
        - Uses different interfaces to sklearn in some functions

    Args:
        estimator: The estimator to be used in the active learning loop.
        query_strategy: Function providing the query strategy for the active learning loop,
            for instance, modAL.uncertainty.uncertainty_sampling.
        on_transformed: Whether to transform samples with the pipeline defined by the estimator
            when applying the query strategy.
        **fit_kwargs: keyword arguments.

    Attributes:
        estimator: The estimator to be used in the active learning loop.
        query_strategy: Function providing the query strategy for the active learning loop.
    """

    def __init__(self,
                 estimator: BaseEstimator,
                 query_strategy: Callable = uncertainty_sampling,
                 on_transformed: bool = False,
                 **fit_kwargs
                 ) -> None:
        #TODO: Check if given query strategy works for Deep Learning
        super().__init__(estimator, query_strategy, on_transformed, **fit_kwargs)

        self.estimator.initialize() # does maybe just work with pytorch

    def fit(self, X: modALinput, y: modALinput, bootstrap: bool = False, **fit_kwargs) -> 'BaseLearner':
        """
        Interface for the fit method of the predictor. Fits the predictor to the supplied data, then stores it
        internally for the active learning loop.

        Args:
            X: The samples to be fitted.
            y: The corresponding labels.
            bootstrap: If true, trains the estimator on a set bootstrapped from X.
                Useful for building Committee models with bagging.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.

        Returns:
            self
        """            
        return self._fit_on_new(X, y, bootstrap=bootstrap, **fit_kwargs)

    def score(self, X: modALinput, y: modALinput) -> Any:
        """
        Interface for the score method of the predictor.

        Args:
            X: The samples for which prediction accuracy is to be calculated.
            y: Ground truth labels for X.

        Returns:
            The score of the predictor.
        """
        """
            sklearn does only accept tensors of different dim for X and Y, if we use
            Multilabel classifiaction. Using tensors of different sizes for more complex models (e.g. Transformers) 
            requires to bypass the sklearn checks by directly calling the NeuralNets infer() function.
        """
        prediction = self.estimator.infer(X)
        criterion = self.estimator.criterion()
        return criterion(prediction, y).item()

    def teach(self, X: modALinput, y: modALinput, warm_start: bool = True, bootstrap: bool = False, **fit_kwargs) -> None:
        """
        Adds X and y to the known training data and retrains the predictor with the augmented dataset.

        Args:
            X: The new samples for which the labels are supplied by the expert.
            y: Labels corresponding to the new instances in X.
            warm_start: If False, the model parameters are resetted and the training starts from zero, 
                otherwise the pre trained model is kept and further trained.
            bootstrap: If True, training is done on a bootstrapped dataset. Useful for building Committee models
                with bagging.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """

        if warm_start: 
            if not bootstrap: 
                self.estimator.partial_fit(X, y, **fit_kwargs)
            else:
                bootstrap_idx = np.random.choice(range(X.shape[0]), X.shape[0], replace=True)
                self.estimator.partial_fit(X[bootstrap_idx], y[bootstrap_idx], **fit_kwargs)
        else: 
            self._fit_on_new(X, y, bootstrap=bootstrap, **fit_kwargs)

"""
Classes for Bayesian optimization
---------------------------------
"""


class BayesianOptimizer(ActiveLearner):
    """
    This class is an abstract model of a Bayesian optimizer algorithm.

    Args:
        estimator: The estimator to be used in the Bayesian optimization. (For instance, a
            GaussianProcessRegressor.)
        query_strategy: Function providing the query strategy for Bayesian optimization,
            for instance, modAL.acquisitions.max_EI.
        X_training: Initial training samples, if available.
        y_training: Initial training labels corresponding to initial training samples.
        bootstrap_init: If initial training data is available, bootstrapping can be done during the first training.
            Useful when building Committee models with bagging.
        **fit_kwargs: keyword arguments.

    Attributes:
        estimator: The estimator to be used in the Bayesian optimization.
        query_strategy: Function providing the query strategy for Bayesian optimization.
        X_training: If the model hasn't been fitted yet it is None, otherwise it contains the samples
            which the model has been trained on.
        y_training: The labels corresponding to X_training.
        X_max: argmax of the function so far.
        y_max: Max of the function so far.

    Examples:

        >>> import numpy as np
        >>> from functools import partial
        >>> from sklearn.gaussian_process import GaussianProcessRegressor
        >>> from sklearn.gaussian_process.kernels import Matern
        >>> from modAL.models import BayesianOptimizer
        >>> from modAL.acquisition import optimizer_PI, optimizer_EI, optimizer_UCB, max_PI, max_EI, max_UCB
        >>>
        >>> # generating the data
        >>> X = np.linspace(0, 20, 1000).reshape(-1, 1)
        >>> y = np.sin(X)/2 - ((10 - X)**2)/50 + 2
        >>>
        >>> # assembling initial training set
        >>> X_initial, y_initial = X[150].reshape(1, -1), y[150].reshape(1, -1)
        >>>
        >>> # defining the kernel for the Gaussian process
        >>> kernel = Matern(length_scale=1.0)
        >>>
        >>> tr = 0.1
        >>> PI_tr = partial(optimizer_PI, tradeoff=tr)
        >>> PI_tr.__name__ = 'PI, tradeoff = %1.1f' % tr
        >>> max_PI_tr = partial(max_PI, tradeoff=tr)
        >>>
        >>> acquisitions = zip(
        ...     [PI_tr, optimizer_EI, optimizer_UCB],
        ...     [max_PI_tr, max_EI, max_UCB],
        ... )
        >>>
        >>> for acquisition, query_strategy in acquisitions:
        ...     # initializing the optimizer
        ...     optimizer = BayesianOptimizer(
        ...         estimator=GaussianProcessRegressor(kernel=kernel),
        ...         X_training=X_initial, y_training=y_initial,
        ...         query_strategy=query_strategy
        ...     )
        ...
        ...     for n_query in range(5):
        ...         # query
        ...         query_idx, query_inst = optimizer.query(X)
        ...         optimizer.teach(X[query_idx].reshape(1, -1), y[query_idx].reshape(1, -1))
    """
    def __init__(self,
                 estimator: BaseEstimator,
                 query_strategy: Callable = max_EI,
                 X_training: Optional[modALinput] = None,
                 y_training: Optional[modALinput] = None,
                 bootstrap_init: bool = False,
                 on_transformed: bool = False,
                 **fit_kwargs) -> None:
        super(BayesianOptimizer, self).__init__(estimator, query_strategy,
                                                X_training, y_training, bootstrap_init, on_transformed, **fit_kwargs)
        # setting the maximum value
        if self.y_training is not None:
            max_idx = np.argmax(self.y_training)
            self.X_max = retrieve_rows(self.X_training, max_idx)
            self.y_max = self.y_training[max_idx]
        else:
            self.X_max = None
            self.y_max = -np.inf

    def _set_max(self, X: modALinput, y: modALinput) -> None:
        max_idx = np.argmax(y)
        y_max = y[max_idx]
        if y_max > self.y_max:
            self.y_max = y_max
            self.X_max = retrieve_rows(X, max_idx)

    def get_max(self) -> Tuple:
        """
        Gives the highest value so far.

        Returns:
            The location of the currently best value and the value itself.
        """
        return self.X_max, self.y_max

    def teach(self, X: modALinput, y: modALinput, bootstrap: bool = False, only_new: bool = False, **fit_kwargs) -> None:
        """
        Adds X and y to the known training data and retrains the predictor with the augmented dataset. This method also
        keeps track of the maximum value encountered in the training data.

        Args:
            X: The new samples for which the values are supplied.
            y: Values corresponding to the new instances in X.
            bootstrap: If True, training is done on a bootstrapped dataset. Useful for building Committee models with
                bagging. (Default value = False)
            only_new: If True, the model is retrained using only X and y, ignoring the previously provided examples.
                Useful when working with models where the .fit() method doesn't retrain the model from scratch (for
                example, in tensorflow or keras).
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """
        self._add_training_data(X, y)

        if not only_new:
            self._fit_to_known(bootstrap=bootstrap, **fit_kwargs)
        else:
            self._fit_on_new(X, y, bootstrap=bootstrap, **fit_kwargs)

        self._set_max(X, y)


"""
Classes for committee based algorithms
--------------------------------------
"""


class Committee(BaseCommittee):
    """
    This class is an abstract model of a committee-based active learning algorithm.

    Args:
        learner_list: A list of ActiveLearners forming the Committee.
        query_strategy: Query strategy function. Committee supports disagreement-based query strategies from
            :mod:`modAL.disagreement`, but uncertainty-based ones from :mod:`modAL.uncertainty` are also supported.
        on_transformed: Whether to transform samples with the pipeline defined by each learner's estimator
            when applying the query strategy.

    Attributes:
        classes_: Class labels known by the Committee.
        n_classes_: Number of classes known by the Committee.

    Examples:

        >>> from sklearn.datasets import load_iris
        >>> from sklearn.neighbors import KNeighborsClassifier
        >>> from sklearn.ensemble import RandomForestClassifier
        >>> from modAL.models import ActiveLearner, Committee
        >>>
        >>> iris = load_iris()
        >>>
        >>> # initialize ActiveLearners
        >>> learner_1 = ActiveLearner(
        ...     estimator=RandomForestClassifier(),
        ...     X_training=iris['data'][[0, 50, 100]], y_training=iris['target'][[0, 50, 100]]
        ... )
        >>> learner_2 = ActiveLearner(
        ...     estimator=KNeighborsClassifier(n_neighbors=3),
        ...     X_training=iris['data'][[1, 51, 101]], y_training=iris['target'][[1, 51, 101]]
        ... )
        >>>
        >>> # initialize the Committee
        >>> committee = Committee(
        ...     learner_list=[learner_1, learner_2]
        ... )
        >>>
        >>> # querying for labels
        >>> query_idx, query_sample = committee.query(iris['data'])
        >>>
        >>> # ...obtaining new labels from the Oracle...
        >>>
        >>> # teaching newly labelled examples
        >>> committee.teach(
        ...     X=iris['data'][query_idx].reshape(1, -1),
        ...     y=iris['target'][query_idx].reshape(1, )
        ... )
    """
    def __init__(self, learner_list: List[ActiveLearner], query_strategy: Callable = vote_entropy_sampling,
                 on_transformed: bool = False) -> None:
        super().__init__(learner_list, query_strategy, on_transformed)
        self._set_classes()
        # TODO: update training data when using fit() and teach() methods
        self.X_training = None
    
    def _add_training_data(self, X: modALinput, y: modALinput) -> None:
        """
        Adds the new data and label to the known data for each learner, but does not retrain the model.

        Args:
            X: The new samples for which the labels are supplied by the expert.
            y: Labels corresponding to the new instances in X.

        Note:
            If the learners have been fitted, the features in X have to agree with the training samples which the
            classifier has seen.
        """
        for learner in self.learner_list:
            learner._add_training_data(X, y)
    
    def _fit_to_known(self, bootstrap: bool = False, **fit_kwargs) -> None:
        """
        Fits all learners to the training data and labels provided to it so far.

        Args:
            bootstrap: If True, each estimator is trained on a bootstrapped dataset. Useful when
                using bagging to build the ensemble.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """
        for learner in self.learner_list:
            learner._fit_to_known(bootstrap=bootstrap, **fit_kwargs)
    
    def fit(self, X: modALinput, y: modALinput, **fit_kwargs) -> None:
        """
        Fits every learner to a subset sampled with replacement from X. Calling this method makes the learner forget the
        data it has seen up until this point and replaces it with X! If you would like to perform bootstrapping on each
        learner using the data it has seen, use the method .rebag()!

        Calling this method makes the learner forget the data it has seen up until this point and replaces it with X!

        Args:
            X: The samples to be fitted on.
            y: The corresponding labels.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """
        for learner in self.learner_list:
            learner.fit(X, y, **fit_kwargs)
        
        self._set_classes()

    def rebag(self, **fit_kwargs) -> None:
        """
        Refits every learner with a dataset bootstrapped from its training instances. Contrary to .bag(), it bootstraps
        the training data for each learner based on its own examples.

        Todo:
            Where is .bag()?

        Args:
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """
        self._fit_to_known(bootstrap=True, **fit_kwargs)

    def teach(self, X: modALinput, y: modALinput, bootstrap: bool = False, only_new: bool = False, **fit_kwargs) -> None:
        """
        Adds X and y to the known training data for each learner and retrains learners with the augmented dataset.

        Args:
            X: The new samples for which the labels are supplied by the expert.
            y: Labels corresponding to the new instances in X.
            bootstrap: If True, trains each learner on a bootstrapped set. Useful when building the ensemble by bagging.
            only_new: If True, the model is retrained using only X and y, ignoring the previously provided examples.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """
        self._add_training_data(X, y)
        if not only_new:
            self._fit_to_known(bootstrap=bootstrap, **fit_kwargs)
        else:
            self._fit_on_new(X, y, bootstrap=bootstrap, **fit_kwargs)
        self._set_classes()

    def predict(self, X: modALinput, **predict_proba_kwargs) -> Any:
        """
        Predicts the class of the samples by picking the consensus prediction.

        Args:
            X: The samples to be predicted.
            **predict_proba_kwargs: Keyword arguments to be passed to the :meth:`predict_proba` of the Committee.

        Returns:
            The predicted class labels for X.
        """
        # getting average certainties
        proba = self.predict_proba(X, **predict_proba_kwargs)
        # finding the sample-wise max probability
        max_proba_idx = np.argmax(proba, axis=1)
        # translating label indices to labels
        return self.classes_[max_proba_idx]

    def predict_proba(self, X: modALinput, **predict_proba_kwargs) -> Any:
        """
        Consensus probabilities of the Committee.

        Args:
            X: The samples for which the class probabilities are to be predicted.
            **predict_proba_kwargs: Keyword arguments to be passed to the :meth:`predict_proba` of the Committee.

        Returns:
            Class probabilities for X.
        """
        return np.mean(self.vote_proba(X, **predict_proba_kwargs), axis=1)

    def score(self, X: modALinput, y: modALinput, sample_weight: List[float] = None) -> Any:
        """
        Returns the mean accuracy on the given test data and labels.

        Todo:
            Why accuracy?

        Args:
            X: The samples to score.
            y: Ground truth labels corresponding to X.
            sample_weight: Sample weights.

        Returns:
            Mean accuracy of the classifiers.
        """
        y_pred = self.predict(X)
        return accuracy_score(y, y_pred, sample_weight=sample_weight)

    def vote(self, X: modALinput, **predict_kwargs) -> Any:
        """
        Predicts the labels for the supplied data for each learner in the Committee.

        Args:
            X: The samples to cast votes.
            **predict_kwargs: Keyword arguments to be passed to the :meth:`predict` of the learners.

        Returns:
            The predicted class for each learner in the Committee and each sample in X.
        """
        prediction = np.zeros(shape=(X.shape[0], len(self.learner_list)))

        for learner_idx, learner in enumerate(self.learner_list):
            prediction[:, learner_idx] = learner.predict(X, **predict_kwargs)

        return prediction

    def vote_proba(self, X: modALinput, **predict_proba_kwargs) -> Any:
        """
        Predicts the probabilities of the classes for each sample and each learner.

        Args:
            X: The samples for which class probabilities are to be calculated.
            **predict_proba_kwargs: Keyword arguments for the :meth:`predict_proba` of the learners.

        Returns:
            Probabilities of each class for each learner and each instance.
        """

        # get dimensions
        n_samples = X.shape[0]
        n_learners = len(self.learner_list)
        proba = np.zeros(shape=(n_samples, n_learners, self.n_classes_))

        # checking if the learners in the Committee know the same set of class labels
        if check_class_labels(*[learner.estimator for learner in self.learner_list]):
            # known class labels are the same for each learner
            # probability prediction is straightforward

            for learner_idx, learner in enumerate(self.learner_list):
                proba[:, learner_idx, :] = learner.predict_proba(X, **predict_proba_kwargs)

        else:
            for learner_idx, learner in enumerate(self.learner_list):
                proba[:, learner_idx, :] = check_class_proba(
                    proba=learner.predict_proba(X, **predict_proba_kwargs),
                    known_labels=learner.estimator.classes_,
                    all_labels=self.classes_
                )

        return proba


class DeepCommittee(BaseCommittee):
    """
    This class is for committee-based deep active learner algorithms.

    Args:
        learner_list: A list of ActiveLearners forming the Committee.
        query_strategy: Query strategy function. Committee supports disagreement-based query strategies from
            :mod:`modAL.disagreement`, but uncertainty-based ones from :mod:`modAL.uncertainty` are also supported.
        on_transformed: Whether to transform samples with the pipeline defined by each learner's estimator
            when applying the query strategy.

    Attributes:
        classes_: Class labels known by the Committee.
        n_classes_: Number of classes known by the Committee.

    Examples:

        >>> from sklearn.datasets import load_iris
        >>> from sklearn.neighbors import KNeighborsClassifier
        >>> from sklearn.ensemble import RandomForestClassifier
        >>> from modAL.models import ActiveLearner, Committee
        >>>
        >>> iris = load_iris()
        >>>
        >>> # initialize ActiveLearners
        >>> learner_1 = ActiveLearner(
        ...     estimator=RandomForestClassifier(),
        ...     X_training=iris['data'][[0, 50, 100]], y_training=iris['target'][[0, 50, 100]]
        ... )
        >>> learner_2 = ActiveLearner(
        ...     estimator=KNeighborsClassifier(n_neighbors=3),
        ...     X_training=iris['data'][[1, 51, 101]], y_training=iris['target'][[1, 51, 101]]
        ... )
        >>>
        >>> # initialize the Committee
        >>> committee = Committee(
        ...     learner_list=[learner_1, learner_2]
        ... )
        >>>
        >>> # querying for labels
        >>> query_idx, query_sample = committee.query(iris['data'])
        >>>
        >>> # ...obtaining new labels from the Oracle...
        >>>
        >>> # teaching newly labelled examples
        >>> committee.teach(
        ...     X=iris['data'][query_idx].reshape(1, -1),
        ...     y=iris['target'][query_idx].reshape(1, )
        ... )
    """
    def __init__(self, learner_list: List[DeepActiveLearner], query_strategy: Callable = vote_entropy_sampling,
                 on_transformed: bool = False) -> None:
        super().__init__(learner_list, query_strategy, on_transformed)
        self._set_classes()
        # TODO: update training data when using fit() and teach() methods
        self.X_training = None
    
    def fit(self, X: modALinput, y: modALinput, **fit_kwargs) -> None:
        """
        Fits every learner to a subset sampled with replacement from X. Calling this method makes the learner forget the
        data it has seen up until this point and replaces it with X! If you would like to perform bootstrapping on each
        learner using the data it has seen, use the method .rebag()!

        Calling this method makes the learner forget the data it has seen up until this point and replaces it with X!

        Args:
            X: The samples to be fitted on.
            y: The corresponding labels.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """
        for learner in self.learner_list:
            learner.fit(X, y, **fit_kwargs)
        
        self._set_classes()

    def teach(self, X: modALinput, y: modALinput, bootstrap: bool = False, **fit_kwargs) -> None:
        """
        Adds X and y to the known training data for each learner and retrains learners with the augmented dataset.

        Args:
            X: The new samples for which the labels are supplied by the expert.
            y: Labels corresponding to the new instances in X.
            bootstrap: If True, trains each learner on a bootstrapped set. Useful when building the ensemble by bagging.
            only_new: If True, the model is retrained using only X and y, ignoring the previously provided examples.
            **fit_kwargs: Keyword arguments to be passed to the fit method of the predictor.
        """
        self._fit_on_new(X, y, bootstrap=bootstrap, **fit_kwargs)
        self._set_classes()

    def predict(self, X: modALinput, **predict_proba_kwargs) -> Any:
        """
        Predicts the class of the samples by picking the consensus prediction.

        Args:
            X: The samples to be predicted.
            **predict_proba_kwargs: Keyword arguments to be passed to the :meth:`predict_proba` of the Committee.

        Returns:
            The predicted class labels for X.
        """
        # getting average certainties
        proba = self.predict_proba(X, **predict_proba_kwargs)
        # finding the sample-wise max probability
        max_proba_idx = np.argmax(proba, axis=1)
        # translating label indices to labels
        return self.classes_[max_proba_idx]

    def predict_proba(self, X: modALinput, **predict_proba_kwargs) -> Any:
        """
        Consensus probabilities of the Committee.

        Args:
            X: The samples for which the class probabilities are to be predicted.
            **predict_proba_kwargs: Keyword arguments to be passed to the :meth:`predict_proba` of the Committee.

        Returns:
            Class probabilities for X.
        """
        return np.mean(self.vote_proba(X, **predict_proba_kwargs), axis=1)

    def score(self, X: modALinput, y: modALinput, sample_weight: List[float] = None) -> Any:
        """
        TODO test with deep learning ... I think it needs still adaption
        Returns the mean accuracy on the given test data and labels.

        Todo:
            Why accuracy?

        Args:
            X: The samples to score.
            y: Ground truth labels corresponding to X.
            sample_weight: Sample weights.

        Returns:
            Mean accuracy of the classifiers.
        """
        y_pred = self.predict(X)
        return accuracy_score(y, y_pred, sample_weight=sample_weight)

    def vote(self, X: modALinput, **predict_kwargs) -> Any:
        """
        Predicts the labels for the supplied data for each learner in the Committee.

        Args:
            X: The samples to cast votes.
            **predict_kwargs: Keyword arguments to be passed to the :meth:`predict` of the learners.

        Returns:
            The predicted class for each learner in the Committee and each sample in X.
        """
        prediction = np.zeros(shape=(X.shape[0], len(self.learner_list)))

        for learner_idx, learner in enumerate(self.learner_list):
            prediction[:, learner_idx] = learner.predict(X, **predict_kwargs)

        return prediction

    def vote_proba(self, X: modALinput, **predict_proba_kwargs) -> Any:
        """
        Predicts the probabilities of the classes for each sample and each learner.

        Args:
            X: The samples for which class probabilities are to be calculated.
            **predict_proba_kwargs: Keyword arguments for the :meth:`predict_proba` of the learners.

        Returns:
            Probabilities of each class for each learner and each instance.
        """

        # get dimensions
        n_samples = X.shape[0]
        n_learners = len(self.learner_list)
        proba = np.zeros(shape=(n_samples, n_learners, self.n_classes_))

        # checking if the learners in the Committee know the same set of class labels
        if check_class_labels(*[learner.estimator for learner in self.learner_list]):
            # known class labels are the same for each learner
            # probability prediction is straightforward

            for learner_idx, learner in enumerate(self.learner_list):
                proba[:, learner_idx, :] = learner.predict_proba(X, **predict_proba_kwargs)

        else:
            for learner_idx, learner in enumerate(self.learner_list):
                proba[:, learner_idx, :] = check_class_proba(
                    proba=learner.predict_proba(X, **predict_proba_kwargs),
                    known_labels=learner.estimator.classes_,
                    all_labels=self.classes_
                )

        return proba


class CommitteeRegressor(BaseCommittee):
    """
    This class is an abstract model of a committee-based active learning regression.

    Args:
        learner_list: A list of ActiveLearners forming the CommitteeRegressor.
        query_strategy: Query strategy function.
        on_transformed: Whether to transform samples with the pipeline defined by each learner's estimator
            when applying the query strategy.

    Examples:

        >>> import numpy as np
        >>> import matplotlib.pyplot as plt
        >>> from sklearn.gaussian_process import GaussianProcessRegressor
        >>> from sklearn.gaussian_process.kernels import WhiteKernel, RBF
        >>> from modAL.models import ActiveLearner, CommitteeRegressor
        >>>
        >>> # generating the data
        >>> X = np.concatenate((np.random.rand(100)-1, np.random.rand(100)))
        >>> y = np.abs(X) + np.random.normal(scale=0.2, size=X.shape)
        >>>
        >>> # initializing the regressors
        >>> n_initial = 10
        >>> kernel = RBF(length_scale=1.0, length_scale_bounds=(1e-2, 1e3)) + WhiteKernel(noise_level=1, noise_level_bounds=(1e-10, 1e+1))
        >>>
        >>> initial_idx = list()
        >>> initial_idx.append(np.random.choice(range(100), size=n_initial, replace=False))
        >>> initial_idx.append(np.random.choice(range(100, 200), size=n_initial, replace=False))
        >>> learner_list = [ActiveLearner(
        ...                         estimator=GaussianProcessRegressor(kernel),
        ...                         X_training=X[idx].reshape(-1, 1), y_training=y[idx].reshape(-1, 1)
        ...                 )
        ...                 for idx in initial_idx]
        >>>
        >>> # query strategy for regression
        >>> def ensemble_regression_std(regressor, X):
        ...     _, std = regressor.predict(X, return_std=True)
        ...     return np.argmax(std)
        >>>
        >>> # initializing the CommitteeRegressor
        >>> committee = CommitteeRegressor(
        ...     learner_list=learner_list,
        ...     query_strategy=ensemble_regression_std
        ... )
        >>>
        >>> # active regression
        >>> n_queries = 10
        >>> for idx in range(n_queries):
        ...     query_idx, query_instance = committee.query(X.reshape(-1, 1))
        ...     committee.teach(X[query_idx].reshape(-1, 1), y[query_idx].reshape(-1, 1))
    """
    def __init__(self, learner_list: List[ActiveLearner], query_strategy: Callable = max_std_sampling,
                 on_transformed: bool = False) -> None:
        super().__init__(learner_list, query_strategy, on_transformed)

    def predict(self, X: modALinput, return_std: bool = False, **predict_kwargs) -> Any:
        """
        Predicts the values of the samples by averaging the prediction of each regressor.

        Args:
            X: The samples to be predicted.
            **predict_kwargs: Keyword arguments to be passed to the :meth:`vote` method of the CommitteeRegressor.

        Returns:
            The predicted class labels for X.
        """
        vote = self.vote(X, **predict_kwargs)
        if not return_std:
            return np.mean(vote, axis=1)
        else:
            return np.mean(vote, axis=1), np.std(vote, axis=1)

    def vote(self, X: modALinput, **predict_kwargs):
        """
        Predicts the values for the supplied data for each regressor in the CommitteeRegressor.

        Args:
            X: The samples to cast votes.
            **predict_kwargs: Keyword arguments to be passed to :meth:`predict` of the learners.

        Returns:
            The predicted value for each regressor in the CommitteeRegressor and each sample in X.
        """
        prediction = np.zeros(shape=(len(X), len(self.learner_list)))

        for learner_idx, learner in enumerate(self.learner_list):
            prediction[:, learner_idx] = learner.predict(X, **predict_kwargs).reshape(-1, )

        return prediction
