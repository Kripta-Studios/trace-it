"""Isolated browser-test server. Never imported by the production application."""

import asyncio
import hashlib
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import HTTPException
from sqlalchemy.engine import make_url

from app.core.config import settings as backend_settings
from app.core.database import session_factory
from app.features.ingestion.config import Settings
from app.features.ingestion.service import ExtractionService
from app.features.ingestion.tests.conftest import NoOCR, NoVLM, pdf_bytes
from app.features.mail_ingestion.config import MailSettings
from app.features.mail_ingestion.documents import RejectedDocument
from app.features.mail_ingestion.imap import Mailbox
from app.features.mail_ingestion.model import MailAccount
from app.features.mail_ingestion.tests.test_pipeline import seed_process
from app.features.mail_ingestion.worker import Worker
from app.features.processes.model import Process
from app.features.users.model import User
from app.main import app
from tests.support.mailbox import LocalMailbox, synthetic_mail
from tests.support.prepare_db import is_test_db

if not is_test_db(make_url(backend_settings.database_url).database):
    raise RuntimeError("Mail browser fixture requires an isolated test database")

original_lifespan = app.router.lifespan_context
fixture = {}


class RetryMailbox(Mailbox):
    def download(self, uid, part):
        if part["original_name"] == "retry.pdf" and fixture.pop("fail_once", False):
            raise RejectedDocument("invalid_pdf")
        return super().download(uid, part)


@asynccontextmanager
async def lifespan(application):
    with tempfile.TemporaryDirectory(prefix="trace-mail-test-") as directory:
        directory = Path(directory)
        mailbox = LocalMailbox(directory)
        password = directory / "password"
        password.write_text("synthetic-test-only")
        token = uuid.uuid4().hex + uuid.uuid4().hex
        async with session_factory() as session:
            process_id = await seed_process(session)
            process = await session.get(Process, process_id)
            process.gathering_email = None  # The browser must assign it.
            user = User(
                name="Mail test manager", email="mail-browser@example.invalid", role="manager"
            )
            session.add(user)
            session.add(
                MailAccount(
                    process_id=process_id,
                    host="localhost",
                    username="migration-test@j-aautomation.com",
                    folder="INBOX",
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                )
            )
            await session.commit()
        app.state.ingestion_service = ExtractionService(
            Settings(
                data_dir=directory / "data",
                model_dir=directory / "models",
                ocr_profile="experimental",
                vlm_api_key=None,
                gemini_api_key=None,
                jev_api_key=None,
                helmcode_api_key=None,
            ),
            NoOCR(),
            NoVLM(),
        )
        # Trust only the locally generated fixture CA. TLS/hostname validation stays on.
        previous_ca = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(mailbox.cert_path)
        cfg = MailSettings(
            ingestion_enabled=True,
            imap_host="localhost",
            imap_port=mailbox.port,
            imap_username="migration-test@j-aautomation.com",
            imap_password_file=password,
            process_id=process_id,
            poll_interval_seconds=1,
        )
        async with httpx.AsyncClient(
            base_url=os.environ["MAIL_TEST_API_URL"],
            headers={"Authorization": "Bearer " + token},
            timeout=60,
            trust_env=False,
        ) as api:
            worker = Worker(cfg, api, mailbox_factory=RetryMailbox)
            fixture.update(
                mailbox=mailbox,
                worker=worker,
                process_id=process_id,
                user_id=user.id,
                task=None,
                before=None,
            )
            async with original_lifespan(application):
                try:
                    yield
                finally:
                    worker.stopping.set()
                    if fixture["task"]:
                        await fixture["task"]
            mailbox.close()
            if previous_ca is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = previous_ca


app.router.lifespan_context = lifespan


@app.get("/_mail-test/state")
async def state():
    box = fixture["mailbox"]
    return {
        "process_id": fixture["process_id"],
        "user_id": fixture["user_id"],
        "email": "mail-browser@example.invalid",
        "forbidden": box.forbidden,
        "unchanged": fixture["before"] == (box.messages, box.flags) if fixture["before"] else True,
    }


@app.post("/_mail-test/start")
async def start():
    if fixture["task"]:
        raise HTTPException(409, "Already started")
    await fixture["worker"].initialize()
    fixture["task"] = asyncio.create_task(fixture["worker"].run())
    return {"started": True}


@app.post("/_mail-test/deliver")
async def deliver():
    box = fixture["mailbox"]
    fixture["pay_pdf"] = pdf_bytes("Holder: open")
    box.deliver(
        synthetic_mail(
            [
                ("pay.pdf", fixture["pay_pdf"], "pdf"),
                ("deny.pdf", pdf_bytes("Holder: paid"), "octet-stream"),
                ("review.pdf", pdf_bytes("Holder: review"), "pdf"),
            ],
            subject="Synthetic automatic invoice batch",
        ),
        seen=True,
    )
    fixture["before"] = dict(box.messages), dict(box.flags)
    return {"delivered": True}


@app.post("/_mail-test/deliver-duplicates")
async def deliver_duplicates():
    box = fixture["mailbox"]
    box.deliver(
        synthetic_mail(
            [("same-receipt.pdf", fixture["pay_pdf"], "pdf")],
            subject="Duplicate receipt must retain its original decision",
        )
    )
    box.deliver(synthetic_mail([], subject="Informational message without attachments"))
    fixture["before"] = dict(box.messages), dict(box.flags)
    return {"delivered": True}


@app.post("/_mail-test/deliver-retry")
async def deliver_retry():
    box = fixture["mailbox"]
    fixture["fail_once"] = True
    box.deliver(
        synthetic_mail(
            [("retry.pdf", pdf_bytes("Holder: retry"), "pdf")], subject="Retry one attachment"
        )
    )
    fixture["before"] = dict(box.messages), dict(box.flags)
    return {"delivered": True}
