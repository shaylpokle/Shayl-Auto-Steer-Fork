"""This module implements a generic but naive approach to approximate the query span. A system integration will be much more efficient."""
import queue
from multiprocessing.pool import ThreadPool as Pool
import numpy as np
import storage
from custom_logging import autosteer_logging
from config import read_config

N_THREADS = int(read_config()['anysteer']['explain_threads'])
FAILED = 'FAILED'


class HintSet:
    """A hint-set describing the knobs to disable and having potential dependencies to other hint-sets"""

    def __init__(self, knobs, dependencies):
        self.knobs: set = knobs
        self.dependencies: HintSet = dependencies
        self.plan = None  # store the json query plan
        self.required = False
        self.predicted_runtime = -1.0

    def get_all_knobs(self) -> list:
        """Return all (including the dependent) knobs"""
        return list(self.knobs) + (self.dependencies.get_all_knobs() if self.dependencies is not None else [])

    def __str__(self):
        res = '' if self.dependencies is None else (',' + str(self.dependencies))
        return ','.join(self.knobs) + res


def get_query_plan(args: tuple) -> HintSet:
    # todo use multiple sessions to explain plans in parallel?
    connector_type, sql_query, hintset = args
    connector = connector_type()
    knobs = hintset.get_all_knobs()
    connector.set_disabled_knobs(knobs)
    hintset.plan = connector.explain(sql_query)
    return hintset


def flatten(l):
    return [item for sublist in l for item in sublist]


def approximate_query_span(connector_type, sql_query: str, get_json_query_plan, find_alternative_rules=False, batch_wise=False) -> \
    list[HintSet]:
    # create singleton hint-sets
    knobs = np.array(connector_type.get_knobs())
    hintsets = np.array([HintSet({knob}, None) for knob in knobs])
    with Pool(N_THREADS) as thread_pool:
        query_span: list[HintSet] = []
        default_plan = get_json_query_plan((connector_type, sql_query, HintSet(set(), None)))
        query_span.append(default_plan)

        args = [(connector_type, sql_query, knob) for knob in hintsets]
        results = np.array(list(map(get_json_query_plan, args)))

        default_plan_hash = hash(default_plan.plan)
        autosteer_logging.info('default plan hash: #%s', default_plan_hash)
        failed_plan_hash = hash(FAILED)
        autosteer_logging.info('failed query hash: #%s', failed_plan_hash)

        hashes = np.array(list(thread_pool.map(lambda res: hash(res.plan), results)))
        effective_optimizers_indexes = np.where((hashes != default_plan_hash) & (hashes != failed_plan_hash))
        required_optimizers_indexes = np.where(hashes == failed_plan_hash)
        autosteer_logging.info('there are %s alternative plans', effective_optimizers_indexes[0].size)

        new_effective_optimizers = queue.Queue()
        for optimizer in results[effective_optimizers_indexes]:
            new_effective_optimizers.put(optimizer)

        required_optimizers = results[required_optimizers_indexes]
        for required_optimizer in required_optimizers:
            required_optimizer.required = True
            query_span.append(required_optimizer)

        # note that indices change after delete
        hintsets = np.delete(hintsets, np.concatenate([effective_optimizers_indexes, required_optimizers_indexes], axis=1))

        if find_alternative_rules:
            if batch_wise:  # batch approximation (this is Pari's approach)
                found_new_optimizers = True
                effective_hint_sets = [results[index] for index in effective_optimizers_indexes[0]]
                disabled_knobs = flatten([hs.knobs for hs in effective_hint_sets])
                all_effective_optimizers = HintSet(set(disabled_knobs), None)

                while found_new_optimizers:
                    for optimizer in results[effective_optimizers_indexes]:
                        query_span.append(optimizer)
                    default_plan = get_json_query_plan((connector_type, sql_query, all_effective_optimizers))
                    default_plan_hash = hash(default_plan.plan)
                    args = [(connector_type, sql_query, HintSet(set(hs.knobs), all_effective_optimizers)) for hs in hintsets]
                    results = np.array(list(thread_pool.map(get_json_query_plan, args)))
                    hashes = np.array(list(map(lambda res: hash(res.plan), results)))
                    effective_optimizers_indexes = np.where((hashes != default_plan_hash) & (hashes != failed_plan_hash))
                    new_alternative_optimizers = results[effective_optimizers_indexes]
                    for new_alternative_optimizer in new_alternative_optimizers:
                        all_effective_optimizers = HintSet(all_effective_optimizers.knobs.union(new_alternative_optimizer.knobs), None)
                    found_new_optimizers = len(effective_optimizers_indexes[0]) > 0

            else:  # iterative approximation
                while not new_effective_optimizers.empty():
                    effective_optimizer = new_effective_optimizers.get()
                    query_span.append(effective_optimizer)
                    default_plan_hash = hash(effective_optimizer.plan)
                    args = [(connector_type, sql_query, HintSet(hs.knobs, effective_optimizer)) for hs in hintsets]
                    results = np.array(list(thread_pool.map(get_json_query_plan, args)))  # thread_pool
                    hashes = np.array(list(map(lambda res: hash(res.plan), results)))
                    effective_optimizers_indexes = np.where((hashes != default_plan_hash) & (hashes != failed_plan_hash))
                    new_alternative_optimizers = results[effective_optimizers_indexes]

                    # add new alternative optimizers to the queue, remove them from the knobs
                    for alternative_optimizer in new_alternative_optimizers:
                        new_effective_optimizers.put(alternative_optimizer)
                        for i in reversed(range(len(hintsets))):
                            if hintsets[i] == alternative_optimizer:
                                hintsets = np.delete(hintsets, i)
                                break
        else:
            while not new_effective_optimizers.empty():
                new_effective_optimizer = new_effective_optimizers.get()
                query_span.append(new_effective_optimizer)
    return query_span


def run_get_query_span(connector_type, query_path):
    autosteer_logging.info('Approximate query span for query: %s', query_path)
    storage.register_query(query_path)

    sql = storage.read_sql_file(query_path)
    query_span = approximate_query_span(connector_type, sql, get_query_plan, find_alternative_rules=False, batch_wise=False)

    # insert the query span into the database
    for optimizer in query_span:  # pylint: disable=not-an-iterable
        autosteer_logging.info('Found new hint-set: %s', optimizer)
        storage.register_optimizer(query_path, ','.join(sorted(optimizer.knobs)), 'query_effective_optimizers')
        # consider recursive optimizer dependencies here
        if optimizer.dependencies is not None:
            storage.register_optimizer_dependency(query_path, ','.join(sorted(optimizer.knobs)), ','.join(sorted(optimizer.knobs)),
                                                  'query_effective_optimizers_dependencies')
