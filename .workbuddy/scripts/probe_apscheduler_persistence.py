"""一次性探测：持久化 jobstore 下 next_run_time 的跨进程保持语义。"""
import os
import shutil
import tempfile

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler


def probe_job():
    return None


def main():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "aps_probe.db")
    url = "sqlite:///" + p.replace("\\", "/")

    # 进程 1：空库注册
    s1 = BackgroundScheduler(timezone="UTC", jobstores={"default": SQLAlchemyJobStore(url=url)})
    print("P1 pre-start lookup (no table):", s1.get_job("probe"))
    s1.add_job("__main__:probe_job", "interval", seconds=300, id="probe",
               replace_existing=True, max_instances=1, coalesce=True)
    s1.start()
    first = s1.get_job("probe").next_run_time
    print("P1 next_run:", first)
    s1.shutdown()

    # 进程 2：不重新 add，直接 start —— next_run 应保持
    s2 = BackgroundScheduler(timezone="UTC", jobstores={"default": SQLAlchemyJobStore(url=url)})
    found = s2.get_job("probe")
    print("P2 pre-start lookup:", found is not None, "| next_run:", getattr(found, "next_run_time", None))
    s2.start()
    print("P2 after start next_run:", s2.get_job("probe").next_run_time)
    s2.shutdown()

    # 进程 3：重新 add replace_existing=True —— next_run 被重置为 now+300s？
    s3 = BackgroundScheduler(timezone="UTC", jobstores={"default": SQLAlchemyJobStore(url=url)})
    s3.add_job("__main__:probe_job", "interval", seconds=300, id="probe",
               replace_existing=True, max_instances=1, coalesce=True)
    s3.start()
    print("P3 (re-added) next_run:", s3.get_job("probe").next_run_time)
    s3.shutdown()

    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
