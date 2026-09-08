from .algorithm import default_config, validate_config, SimpleBBOWorker, BBOParameters, generate_bbo_target

def validate(config, metadata, capabilities):
    return validate_config(config, metadata)

def preview(worker, draft, test_amounts=None, bbo=None):
        draft = validate_config(draft, worker.metadata)
        source = worker.feed.snapshot()
        observation = bbo or source.get("observation")
        if observation is None:
            # Configuration can be enabled before the first live tick. Worker
            # stays paused with zero orders until price AND sizes are valid.
            return {"preflight": {"ok": True}, "source_live": False, "orders": []}
        result = generate_bbo_target(draft, worker.metadata, observation)
        return {**result, "preflight": {"ok": True}, "source_live": bbo is None}
