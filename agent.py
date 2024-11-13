#!/usr/bin/env python3
import collections
import datetime
import json
import pathlib
import socket
import statistics
import struct
import sys
import subprocess
import time
import threading
import traceback


def get_pod_map(namespace, components):
    # name_to_uid = {}
    # p = subprocess.run(['kubectl', 'get', 'pods', f'-n={namespace}',
    #     r'-o=jsonpath={range .items[*]}{.metadata.uid} {.metadata.name}{"\n"}{end}'],
    #     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, check=True)
    # for i in p.stdout.splitlines():
    #     uid, name = i.split()
    #     name = name.rsplit('-', 2)[0]
    #     if name in components:
    #         assert name not in name_to_uid
    #         name_to_uid[name] = uid

    # uid_to_qos = {}
    cgroup = pathlib.Path('/sys/fs/cgroup')
    # for qos in ['guaranteed', 'burstable', 'besteffort']:
    #     d = cgroup/f'cpu/kubepods.slice/kubepods-{qos}.slice'
    #     p = f'kubepods-{qos}-pod'
    #     s = '.slice'
    #     for i in d.glob(f'{p}*{s}'):
    #         uid = i.name[len(p):-len(s)].replace('_', '-')
    #         uid_to_qos[uid] = qos

    pod_map = {}
    # for name in components:
    #     uid = name_to_uid[name]
    #     try:
    #         qos = uid_to_qos[uid]
    #     except KeyError:
    #         pass
    #     else:
    for name in components:
        pod_map[name] = f'docker/{name}/'
    return pod_map


def stat_path(pod_map, name, stat):
    # qos, uid = pod_map[name]
    # family, _, name = stat.partition('.')
    # slices = f'kubepods.slice/kubepods-{qos}.slice/kubepods-{qos}-pod{uid.replace("-", "_")}.slice'
    # return pathlib.Path(f'/sys/fs/cgroup/{family}/{slices}/{family}.{name}')
    group = pod_map[name]
    return pathlib.Path(f'/sys/fs/cgroup/cpu/{group}/{stat}')


def set_cpu_limit(pod_map, name, limit, period=0.1):
    period_us = round(period * 1e6)
    assert 1000 <= period_us <= 1000000
    if limit is None:
        quota_us = -1
    else:
        quota_us = round(limit * period_us)
        assert quota_us >= 1000

    stat_path(pod_map, name, 'cpu.cfs_period_us').write_text(str(period_us))
    stat_path(pod_map, name, 'cpu.cfs_quota_us').write_text(str(quota_us))
    print(f'{datetime.datetime.now()} Written period={period_us},quota={quota_us} to name={name},(qos,uid)={pod_map[name]}')

    return 

class ConstScaler:
    def __init__(self, limit):
        self.limit = limit

    def __call__(self, t, stats):
        return self.limit

    def update(self, limit):
        self.limit = limit


class K8sCPUScalerBase:
    def __init__(self, period, stabilization, target, initial_limit):
        self.period = period
        self.target = target
        self.limit = initial_limit
        self.recommend_len = stabilization // period
        self.recommendations = []

        self.last_t = None
        self.last_stats = None

    def __call__(self, t, stats):
        if self.last_t is None:
            self.last_t = t
            self.last_stats = stats
            return self.limit
        if t < self.last_t + self.period - 0.0001:
            return self.limit
        usage = (stats['cpu_usage'] - self.last_stats['cpu_usage']) / (t - self.last_t)
        self.scale(usage)
        self.last_t = t
        self.last_stats = stats
        return self.limit

    def scale(self, usage):
        self.recommendations.append(usage / self.target)
        if len(self.recommendations) > self.recommend_len:
            self.recommendations.pop(0)
        self.limit = max(self.recommendations)

    def update(self, target):
        self.target = target


class K8sCPUScaler(K8sCPUScalerBase):
    def __init__(self, target, initial_limit=1):
        super().__init__(period=15, stabilization=300, target=target, initial_limit=initial_limit)


class K8sCPUFastScaler(K8sCPUScalerBase):
    def __init__(self, target, initial_limit=1):
        super().__init__(period=1, stabilization=20, target=target, initial_limit=initial_limit)


class CaptainScaler:
    def __init__(self, target, initial_limit=1):
        # read-only parameters
        # self.target = 0.0 # Force captain to try minimize throttling rate
        self.target = 0.001
        self.period = 1

        # state
        self.limit = initial_limit
        self.last_limit = initial_limit
        self.throttled_history = [0 for _ in range(int(10 * self.period))]
        self.usage_history = [0.0 for _ in range(50)]
        self.margin = 3
        self.scale_down_cd = 0
        self.last_scale_down = False

        self.last_t = None
        self.last_stats = None
        self.last_scale_t = None

    def __call__(self, t, stats):
        if self.last_t is None:
            self.last_t = t
            self.last_stats = stats
            self.last_scale_t = t
            return self.limit

        try:
            new_throttled = stats['cpu_stat.nr_throttled'] - self.last_stats['cpu_stat.nr_throttled']
            self.throttled_history.append(new_throttled)
            self.throttled_history.pop(0)
            new_usage = (stats['cpu_usage'] - self.last_stats['cpu_usage']) / (t - self.last_t)
            self.usage_history.append(new_usage)
            self.usage_history.pop(0)
            self.last_t = t
            self.last_stats = stats
        except Exception as e:
            return self.limit

        self.rollback_mechanism()       

        if t < self.last_scale_t + self.period - 0.0001:
            return self.limit

        self.scale_updown() 

        self.throttled_history = [0 for _ in range(int(10 * self.period))]
        self.limit = max(0.01, self.limit)
        self.last_scale_t = t
        stats['captain.margin'] = self.margin


        return self.limit

    def update(self, target):
        self.target = target

    def rollback_mechanism(self):
        print(f'IN ROLLBACK at {self.last_t}')
        throttled_rate = statistics.mean(self.throttled_history)
        if throttled_rate > 3 * self.target and self.last_scale_down:
            print(f'PASS THRESHOLD. Rate: {throttled_rate}, target: {self.target}, threshold: {3 * self.target}')
            self.limit = 2 * self.last_limit - self.limit # 
            self.margin += (throttled_rate - self.target)
            self.throttled_history = [0 for _ in range(int(10 * self.period))]
            self.last_scale_down = False

    def scale_updown(self):
        print(f'IN SCALE at {self.last_t}')
        self.last_limit = self.limit
        throttled_rate = statistics.mean(self.throttled_history)
        usage_max = max(self.usage_history)
        usage_std = statistics.stdev(self.usage_history)

        self.margin += (throttled_rate - self.target)
        self.margin = max(0, self.margin)
        self.last_scale_down = False
        print(f'Throttled rate = {throttled_rate}, thres={3*self.target}')
        if throttled_rate > 3 * self.target:
            self.default_scaleup(throttled_rate)
            # self.additive_scaleup(throttled_rate)
        else:
            # Instantaneously scale down
            # self.default_scaledown(usage_max, usage_std)
            self.additive_scaledown(usage_max, usage_std)
        print(f'At t={self.last_t}, tr_rate={throttled_rate}, limit={self.limit}, last_usage={self.usage_history[-1]}, quotaus={self.last_stats["cpu_cfs_quota_us"]}')

    def default_scaleup(self, throttled_rate):
        # Multiplicative scale up 
        print(f'Multiplicative scale up')
        print(f'Prev limit: {self.limit}')
        self.limit *= 1 + (throttled_rate - 3 * self.target)
        print(f'New limit: {self.limit}')

    def default_scaledown(self, usage_max, usage_std): 
        print(f'Instant scale down')
        usage_limit = usage_max + usage_std * self.margin
        print(f'usage_max={usage_max}, usage_std={usage_std}, margin={self.margin}, usage_limit={usage_limit}, self.limit={self.limit}')
        if usage_limit <= self.limit * 0.9 and self.scale_down_cd == 0:
            print(f'Instance scale down -- usage limit less than threshold and cd==0')
            print(f'Prev limit: {self.limit}')
            self.limit = max(self.limit * 0.5, usage_limit)
            print(f'New limit: {self.limit}')
            self.last_scale_down = True

    def additive_scaleup(self, throttled_rate):
        # Additive scale up 
        print(f'Additive scale up')
        print(f'Prev limit: {self.limit}')
        # self.limit *= 1 + (throttled_rate - 3 * self.target)
        self.limit += 1
        print(f'New limit: {self.limit}')

    def additive_scaledown(self, usage_max, usage_std):
        print(f'Additive scale down')
        usage_limit = usage_max + usage_std * self.margin
        print(f'usage_max={usage_max}, usage_std={usage_std}, margin={self.margin}, usage_limit={usage_limit}, self.limit={self.limit}')
        if usage_limit <= self.limit * 0.9 and self.scale_down_cd == 0:
            print(f'Prev limit: {self.limit}')
            # self.limit = max(self.limit * 0.5, usage_limit)
            self.limit -= 1
            print(f'New limit: {self.limit}')
            self.last_scale_down = True


def init_scaler(data):
    print(f'Init scaler with data={data}')
    return {
        'const': ConstScaler,
        'k8s-cpu-fast': K8sCPUFastScaler,
        'k8s-cpu': K8sCPUScaler,
        'captain': CaptainScaler,
    }[data['type']](*data['params'])


def run(control, namespace, components, scalers):
    pod_map = get_pod_map(namespace, components)

    limits = {}
    for name in scalers:
        assert name in components
    for name in components:
        limits[name] = None
        set_cpu_limit(pod_map, name, None)

    files = {}
    for name in components:
        files[name, 'cpuacct.usage'] = stat_path(pod_map, name, 'cpuacct.usage').open()
        files[name, 'cpu.stat'] = stat_path(pod_map, name, 'cpu.stat').open()
        files[name, 'cpu.cfs_quota_us'] = stat_path(pod_map, name, 'cpu.cfs_quota_us').open()

    monotonic_base = time.time() - time.perf_counter()

    stats_history = collections.defaultdict(list)
    control['stats'] = stats_history

    stats_current = collections.defaultdict(list)
    control['stats_current'] = stats_current

    late_end_time = 0
    while True:
        t = time.perf_counter()
        tt = (0.097 - t) * 1000 % 100 / 1000
        t += tt
        time.sleep(tt)
        if control['stop']:
            break
        
        stats = collections.defaultdict(dict)
        for name in components:
            try:
                files[name, 'cpuacct.usage'].seek(0)
                stats[name]['cpu_usage'] = files[name, 'cpuacct.usage'].read()
                files[name, 'cpu.stat'].seek(0)
                for line in files[name, 'cpu.stat'].read().splitlines():
                    k, v = line.split()
                    stats[name][f'cpu_stat.{k}'] = v
                files[name, 'cpu.cfs_quota_us'].seek(0)
                stats[name]['cpu_cfs_quota_us'] = files[name, 'cpu.cfs_quota_us'].read()
            except Exception as e:
                print(f'At t={t} {name} has exception {e}, skipping ...')

        end_time = time.perf_counter()
        end_limit = (t + (-t * 1000 % 100 / 1000))
        if end_time > t + (-t * 1000 % 100 / 1000):
            late_end_time += 1

        for name in components:
            try:
                stats[name]['cpu_usage'] = int(stats[name]['cpu_usage']) / 1e9
                stats[name]['cpu_stat.nr_periods'] = int(stats[name]['cpu_stat.nr_periods'])
                stats[name]['cpu_stat.nr_throttled'] = int(stats[name]['cpu_stat.nr_throttled'])
                stats[name]['cpu_stat.throttled_time'] = int(stats[name]['cpu_stat.throttled_time']) / 1e9
                stats[name]['cpu_stat.throttled_time'] = int(stats[name]['cpu_stat.throttled_time']) / 1e9
                stats[name]['cpu_cfs_quota_us'] = int(stats[name]['cpu_cfs_quota_us'])
            except Exception as e:
                print(f'At t={t} {name} error {e}')


        if control['update']:
            for k, v in control['update'].items():
                if k in scalers:
                    print(f'At t={t}, scaler={k}, control.update={v}')
                    scalers[k].update(*v)
            control['update'] = {}
        for name, scaler in scalers.items():
            # print(f'At t={t}, calling scaler {scaler.__class__.__name__} for {name}, target={scaler.target} ...')
            limit = scaler(t, stats[name])
            if limit is not None:
                limit = max(0.01, limit)
                if limits[name] is not None:
                    if abs(limit - limits[name]) < 0.00001:
                        limit = limits[name]
            if limit != limits[name]:
                limits[name] = limit
                set_cpu_limit(pod_map, name, limit)
            stats[name]['scaler.limit'] = limits[name]

        for name in stats:
            stats_history[name].append((t + monotonic_base, stats[name]))
            stats_current[name].append((t + monotonic_base, stats[name]))

    for f in files.values():
        f.close()


def process_client(client_socket):
    line = client_socket.readline()
    data = json.loads(line)
    assert data['method'] == 'start'
    control = {
        'stop': False,
        'socket': client_socket,
        'update': {},
    }
    scalers = {k: init_scaler(v) for k, v in data['scalers'].items()}


    thread = threading.Thread(target=run, args=(control, data['namespace'], data['components'], scalers))
    thread.start()
    client_socket.write(json.dumps({'ok': True}) + '\n')
    client_socket.flush()
    try:
        while True:
            line = client_socket.readline()
            data = json.loads(line)
            if data['method'] == 'update':
                control['update'] = data['update']
                client_socket.write(json.dumps({'ok': True}) + '\n')
                client_socket.flush()
                continue
            elif data['method'] == 'stats':
                stats = {}
                for i in control['stats_current']:
                    stats[i] = control['stats_current'][i]
                    control['stats_current'][i] = []
                client_socket.write(json.dumps({
                    'ok': True,
                    'stats': stats,
                }) + '\n')
                client_socket.flush()
                continue
            elif data['method'] == 'stop':
                control['stop'] = True
                thread.join()
                client_socket.write(json.dumps({
                    'ok': True,
                    'stats': control['stats'],
                }) + '\n')
                client_socket.flush()
                client_socket.close()
                break
            raise ValueError(f'unknown method: {data["method"]}')
    except Exception:
        traceback.print_exc()
        control['stop'] = True
        thread.join()
        client_socket.close()
        print('thread stopped')
        print()


def main():
    PORT = int(sys.argv[1])
    assert PORT, 'port not provided'
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(('localhost', PORT))
    server_socket.listen()
    print('listening')
    while True:
        client_socket, address = server_socket.accept()
        print(f'accepted connection from {address}')
        process_client(client_socket.makefile('rw'))
        print('finished')

if __name__ == '__main__':
    main()
    # stub()