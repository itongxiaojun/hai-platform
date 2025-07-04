

from .default import *
from .custom import *


import pickle
from k8s_watcher.base import ListWatcher
from k8s_watcher.utils import v1, custom_v1, module
from datetime import datetime, timedelta, timezone
import pandas as pd
from k8s import get_k8s_dict_val
from dateutil.parser import parse
from conf.flags import QUE_STATUS, TASK_TYPE
from conf import MARS_GROUP_FLAG
from db import MarsDB, redis_conn
from logm import logger, log_stage


# 用来只留需要的列
NODES_DF_COLUMNS = [
    'name', 'status', 'roles', 'mars_group', 'group', 'gpu_num', 'cpu', 'memory', 'nodes', 'cluster', 'type', 'use',
    'room', 'origin_group', 'schedule_zone', 'internal_ip', 'working', 'working_user', 'working_user_role',
    'working_task_id', 'working_task_rank'
]
NODES_DF_COLUMNS += EXTRA_NODES_DF_COLUMNS
if MARS_GROUP_FLAG not in NODES_DF_COLUMNS:
    NODES_DF_COLUMNS.append(MARS_GROUP_FLAG)


class NodeListWatcher(ListWatcher):
    def __init__(self, label_selector=None, field_selector=None, process_interval=10):
        super().__init__('node', custom_v1.list_node, v1.list_node, None, label_selector, field_selector, process_interval)
        self.last_nodes_df = None
        self.count = 0

    @log_stage(module)
    def _get_nodes_df(self):
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        df = pd.DataFrame.from_records([
            {
                'roles': ['node', 'master']['node-role.kubernetes.io/master' in n['metadata'].get('labels', dict())],
                'status': ['Ready', 'NotReady'][
                    n['spec'].get('unschedulable', False) or
                    any(
                        c['type'] == 'Ready' and c['status'].lower() in ['false', 'unknown'] and now - parse(get_k8s_dict_val(c, 'lastTransitionTime')).astimezone(timezone.utc) >= timedelta(seconds=10)
                        for c in n['status']['conditions']
                    )
                ],
                'internal_ip': next((data['address'] for data in n['status']['addresses'] if data['type']=='InternalIP'), None),
                **n['metadata'].get('labels', dict()),
                **n['status'].get('allocatable', dict()),
            } for n in list(self._data.values())
        ])
        df['mars_group'] = df[MARS_GROUP_FLAG].apply(lambda s: s if s == s else None)
        df[MARS_GROUP_FLAG] = df['mars_group']
        g = df.mars_group.str.split('.').str
        df['group'] = g[-1].astype(object).where(g[-1].astype(object).notna(), None)
        df['name'] = df['kubernetes.io/hostname']
        # 从数据库拿 host_info
        hosts_info = {host_info['node']: host_info for host_info in [{**res} for res in MarsDB(overwrite_use_db='secondary').execute('select * from host')]}
        no_host_info_nodes = set(df.name.to_list()) - set(hosts_info.keys())
        if self.count % 1000 == 0:
            if len(no_host_info_nodes) > 0:
                logger.f_warning(f"这些节点没有设置 host_info：{list(no_host_info_nodes)}")
        df.loc[df.name.isin(no_host_info_nodes), 'status'] = 'NotReady'
        df['gpu_num'] = df['name'].apply(lambda n: hosts_info.get(n, {}).get('gpu_num', 0)).astype(int)
        df['type'] = df['name'].apply(lambda n: hosts_info.get(n, {}).get('type', None)).astype(str)
        df['use'] = df['name'].apply(lambda n: hosts_info.get(n, {}).get('use', None)).astype(str)
        df['room'] = df['name'].apply(lambda n: hosts_info.get(n, {}).get('schedule_zone', None)).astype(str)
        df['schedule_zone'] = df['name'].apply(lambda n: hosts_info.get(n, {}).get('schedule_zone', None)).astype(str)
        df['origin_group'] = df['name'].apply(lambda n: hosts_info.get(n, {}).get('origin_group', None)).astype(str)
        df['cpu'] = df.cpu.apply(lambda s: int(s[0:-1]) / 1000 if s[-1:] == 'm' else int(s)).astype(int)
        memory_convert = {
            'E': 1e18,
            'P': 1e15,
            'T': 1e12,
            'G': 1e9,
            'M': 1e6,
            'K': 1e3,
            'Ei': 1 << 60,
            'Pi': 1 << 50,
            'Ti': 1 << 40,
            'Gi': 1 << 30,
            'Mi': 1 << 20,
            'Ki': 1 << 10,
        }
        df['memory'] = df.memory.apply(lambda s: int(s[:-2]) * memory_convert[s[-2:]] if memory_convert.get(s[-2:])
            else (int(s[:-1]) * memory_convert[s[:-1]] if memory_convert.get(s[-1:]) else int(s))).astype(int)
        # 添加调度所需信息
        df['nodes'] = 1
        # 添加 running 信息，background 不算 running
        running_nodes_df = pd.read_sql(f"""
        select 
            distinct "unfinished_task_ng"."id", "unfinished_task_ng"."task_type", "unfinished_task_ng"."user_name",
            "user"."role", "assigned_nodes", case when "unfinished_task_ng"."task_type" = '{TASK_TYPE.TRAINING_TASK}' then 0 else 1 end as "rank"
        from "unfinished_task_ng"
        inner join "user" on "user"."user_name" = "unfinished_task_ng"."user_name"
        where "queue_status" = '{QUE_STATUS.SCHEDULED}' and "task_type" != '{TASK_TYPE.BACKGROUND_TASK}'
        order by "unfinished_task_ng"."id"
        """, MarsDB(overwrite_use_db='secondary').db)
        df['working'] = None
        df['working_user'] = None
        df['working_user_role'] = None
        df['working_task_id'] = None
        df['working_task_rank'] = None
        for _, row in running_nodes_df.sort_values('rank').iterrows():
            df.loc[df.name.isin(row.assigned_nodes), 'working'] = row.task_type
            # training 是独占的，这些字段才有意义
            if row.task_type == TASK_TYPE.TRAINING_TASK:
                df.loc[df.name.isin(row.assigned_nodes), 'working_user'] = row.user_name
                df.loc[df.name.isin(row.assigned_nodes), 'working_user_role'] = row.role
                df.loc[df.name.isin(row.assigned_nodes), 'working_task_id'] = row.id
                for rank, node in enumerate(row.assigned_nodes):
                    df.loc[df.name == node, 'working_task_rank'] = rank
            # 为独占 jupyter 添加 working_user 等字段
            if row.task_type == TASK_TYPE.JUPYTER_TASK:
                mask = df.name.isin(row.assigned_nodes) & df.group.str.endswith('_dedicated', na=False)
                if mask.any():
                    df.loc[mask, 'working_user'] = row.user_name
                    df.loc[mask, 'working_user_role'] = row.role
                    df.loc[mask, 'working_task_id'] = row.id
        df = get_extra_columns(df)
        df = df[NODES_DF_COLUMNS]
        df = df.sort_values(by=['name'], ignore_index=True)
        self.count += 1
        return df.where(df.notnull(), None)

    def process(self):
        nodes_df = self._get_nodes_df()
        if not nodes_df.equals(self.last_nodes_df):
            logger.info(f'set nodes_df_pickle in redis')
            redis_conn.set('nodes_df_pickle', pickle.dumps(nodes_df))  # 这里改成 pickle，json 反序列化 None 可能会变成 NAN
            self.last_nodes_df = nodes_df
