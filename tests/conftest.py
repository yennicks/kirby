from pytest import fixture
import os
from requests_flask_adapter import Session
import tenacity
from kirby.api.ext import kafka_retry_args


from kirby.web import app_maker
from kirby.models import (
    db,
    Environment,
    JobType,
    Job,
    Context,
    Schedule,
    NotificationGroup,
    NotificationEmail,
    Notification,
    Script,
    Topic,
)

API_ROOT = "http://some-test-server.somewhere"


@fixture
def webapp():
    app = app_maker(
        config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        }
    )
    with app.app_context():
        app.try_trigger_before_first_request_functions()
        yield app


@fixture
def data_dir():
    current_dir = os.path.dirname(__file__)
    data_dir = os.path.join(current_dir, "data")
    return data_dir


@fixture
def session(webapp):
    Session.register(API_ROOT, webapp)
    return Session()


@fixture
def db_env(session):
    env = Environment(name="test_env")
    db.session.add(env)
    db.session.commit()
    return env


@fixture
def db_notification_groups(session):
    admin = NotificationGroup(
        name="Admin",
        emails=[NotificationEmail(email="admin@some-test-server.somewhere")],
    )
    producers = NotificationGroup(
        name="Producers",
        emails=[
            NotificationEmail(email="producer1@some-test-server.somewhere"),
            NotificationEmail(email="producer2@some-test-server.somewhere"),
        ],
    )
    bakery = NotificationGroup(
        name="Bakery",
        emails=[NotificationEmail(email="bakery@some-test-server.somewhere")],
    )

    notification_groups = [admin, producers, bakery]
    db.session.add_all(notification_groups)
    db.session.commit()
    return notification_groups


@fixture
def db_jobs(db_notification_groups):
    [admin, producers, bakery] = db_notification_groups
    orders_job = Job(
        name="Fetch Orders",
        type=JobType.SCHEDULED,
        notifications=[
            Notification(on_retry=True, on_failure=True, groups=[admin]),
            Notification(
                on_retry=False, on_failure=True, groups=[producers, bakery]
            ),
        ],
    )
    cashregister_job = Job(
        name="Collect Register",
        type=JobType.TRIGGERED,
        notifications=[
            Notification(on_retry=True, on_failure=True, groups=[admin])
        ],
    )

    prepare_job = Job(
        name="Prepare Daily Orders",
        type=JobType.SCHEDULED,
        notifications=[
            Notification(on_retry=True, on_failure=True, groups=[admin])
        ],
    )
    abort_job = Job(
        name="Stop everything",
        type=JobType.SCHEDULED,
        notifications=[
            Notification(on_retry=True, on_failure=True, groups=[admin])
        ],
    )
    jobs = [orders_job, cashregister_job, prepare_job, abort_job]
    db.session.add_all(jobs)
    db.session.commit()
    return jobs


@fixture
def db_contexts(db_env, db_jobs):
    [orders_job, cashregister_job, prepare_job, abort_job] = db_jobs
    orders_context = Context(
        environment=db_env,
        job=orders_job,
        schedules=[Schedule(name="Every minute")],
    )
    cashregister_context = Context(environment=db_env, job=cashregister_job)
    prepare_context = Context(
        environment=db_env,
        job=prepare_job,
        schedules=[Schedule(name="Every day at 00:00", minute="0", hour="0")],
    )
    abort_context = Context(environment=db_env, job=abort_job)

    contexts = [
        orders_context,
        cashregister_context,
        prepare_context,
        abort_context,
    ]
    db.session.add_all(contexts)
    db.session.commit()
    return contexts


@fixture
def db_scripts_not_registered(db_contexts):
    [
        orders_context,
        cashregister_context,
        prepare_context,
        abort_context,
    ] = db_contexts
    orders_script = Script(
        package_name="orders_retriever",
        package_version="3.1.0",
        context=orders_context,
    )
    cashregister_script = Script(
        package_name="cashregister_retriever",
        package_version="2.0.4",
        context=cashregister_context,
    )
    prepare_script = Script(
        package_name="prepare_order_for_factory",
        package_version="2.2.1",
        context=prepare_context,
    )
    abort_script = Script(
        package_name="abort_all",
        package_version="0.0.1",
        context=abort_context,
    )

    scripts = [
        orders_script,
        cashregister_script,
        prepare_script,
        abort_script,
    ]
    db.session.add_all(scripts)
    db.session.commit()
    return scripts


@fixture
def db_topics(session):
    cashregister = Topic(name="cashregister")
    orders = Topic(name="orders")
    errors_log = Topic(name="errors_log")
    asset_management = Topic(name="asset_management")
    factory = Topic(name="factory")
    timeseries = Topic(name="timeseries")

    topics = [
        cashregister,
        orders,
        errors_log,
        asset_management,
        factory,
        timeseries,
    ]
    db.session.add_all(topics)
    db.session.commit()
    return topics


@fixture
def db_scripts_registered(db_scripts_not_registered, db_topics):
    [
        cashregister,
        orders,
        errors_log,
        asset_management,
        factory,
        timeseries,
    ] = db_topics
    [
        orders_script,
        cashregister_script,
        prepare_script,
        abort_script,
    ] = db_scripts_not_registered

    orders_script.add_source(orders)
    orders_script.add_destination(timeseries)
    orders_script.add_destination(asset_management)

    cashregister_script.add_source(cashregister)
    cashregister_script.add_destination(timeseries)
    cashregister_script.add_destination(asset_management)

    prepare_script.add_source(asset_management)
    prepare_script.add_destination(factory)

    abort_script.add_destination(errors_log)
    db.session.commit()


@fixture
def kafka_topic_factory():
    from smart_getenv import getenv
    from contextlib import contextmanager
    from kafka import KafkaAdminClient
    from kafka.admin import NewTopic
    from kafka.errors import UnknownTopicOrPartitionError
    import logging

    logger = logging.getLogger(__name__)

    bootstrap_servers = getenv(
        "KAFKA_BOOTSTRAP_SERVERS", type=list, separator=","
    )
    if bootstrap_servers:
        args = {"bootstrap_servers": bootstrap_servers}
        if getenv("KAFKA_USE_TLS", type=bool):
            args.update(
                {
                    "security_protocol": "SSL",
                    "ssl_cafile": getenv("KAFKA_SSL_CAFILE"),
                    "ssl_certfile": getenv("KAFKA_SSL_CERTFILE"),
                    "ssl_keyfile": getenv("KAFKA_SSL_KEYFILE"),
                }
            )

        admin = tenacity.retry(**kafka_retry_args)(KafkaAdminClient)(**args)

        @tenacity.retry(**kafka_retry_args)
        @contextmanager
        def create_kafka_topic(topic_name, timeout_ms=1500):
            try:
                admin.delete_topics([topic_name])
            except UnknownTopicOrPartitionError:
                pass

            admin.create_topics(
                [NewTopic(topic_name, 1, 1)], timeout_ms=timeout_ms
            )
            yield

            admin.delete_topics([topic_name])

        yield create_kafka_topic

        admin.close()

    else:
        logger.warning(
            f"There is no KAFKA_BOOTSTRAP_SERVERS. "
            "Creation of kafka_topic skipped."
        )
        yield
