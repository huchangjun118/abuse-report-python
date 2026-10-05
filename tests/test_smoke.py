from abuse import AbuseService, WorkflowConfig


def test_skeleton_constructs():
    svc = AbuseService(WorkflowConfig(db_path=":memory:"))
    svc.close()
