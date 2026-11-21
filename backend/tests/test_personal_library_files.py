from test_resources import resource_store, access_for
from test_auth import register
from app.shared_resources import directory_marker_key


def test_empty_library_has_visible_private_directory(accounts, resource_store):
    (client, other, _), db = accounts
    register(client)
    register(other, 'other-library@example.com')
    actor = access_for(client, db)
    for expected in ('知识库/资料', '知识库/资料 (2)'):
        response = client.post('/knowledge-bases', json={'name': '资料'})
        assert response.status_code == 201, response.text
        assert response.json()['directory_path'] == expected
        assert resource_store[0][directory_marker_key(actor.user_id, expected)] == b''
    def paths(browser, path):
        result = browser.get('/resources/browse', params={'path': path})
        assert result.status_code == 200, result.text
        return {item['path'] for item in result.json()['items']}
    assert '知识库' in paths(client, '')
    assert paths(client, '知识库') == {'知识库/资料', '知识库/资料 (2)'}
    assert '知识库' not in paths(other, '')


def test_pdf_upload_publishes_one_original_and_job(accounts, resource_store, monkeypatch):
    from sqlalchemy import select
    from app.models import IngestionJob
    (client, _, _), db = accounts
    register(client)
    actor = access_for(client, db)
    queued = []
    monkeypatch.setattr('app.main.process_document.delay', queued.append)
    library = client.post('/knowledge-bases', json={'name': '项目资料'}).json()
    pdf = b'%PDF-1.5\nfixture'
    result = client.post(f"/knowledge-bases/{library['id']}/documents", files={'file': ('报价.pdf', pdf, 'application/pdf')})
    assert result.status_code == 201, result.text
    doc = result.json()
    path = '知识库/项目资料/报价.pdf'
    assert client.get('/resources/' + doc['personal_file_id']).json()['path'] == path
    assert resource_store[0][f'accounts/{actor.user_id}/resource-directory/{path}'] == pdf
    assert queued == [str(db.scalars(select(IngestionJob.id)).one())]


def test_mounted_edit_move_copy_and_delete(accounts, resource_store):
    from sqlalchemy import select
    from app.models import Document, DocumentStatus
    from app.personal_library_sync import reconcile
    from app.shared_resources import object_key
    (client, _, _), db = accounts
    register(client)
    actor = access_for(client, db)
    for name in ['甲库', '乙库']:
        assert client.post('/knowledge-bases', json={'name': name}).status_code == 201
    objects = resource_store[0]
    def key(path):
        return object_key(actor.user_id, '知识库/' + path)
    objects[key('甲库/a.txt')] = b'original'
    assert reconcile(db, actor)['added'] == 1
    doc = db.scalars(select(Document)).one()
    identifier = doc.id
    objects[key('乙库/b.txt')] = objects.pop(key('甲库/a.txt'))
    moved = reconcile(db, actor)
    assert moved['moved'] == 1 and not moved['queued_jobs']
    objects[key('乙库/b.txt')] = b'replaced'
    assert reconcile(db, actor)['updated'] == 1
    assert reconcile(db, actor)['queued_jobs'] == []
    assert db.get(Document, identifier).filename == 'b.txt'
    objects[key('乙库/c.txt')] = objects[key('乙库/b.txt')]
    assert reconcile(db, actor)['added'] == 1
    del objects[key('乙库/b.txt')]
    assert reconcile(db, actor)['deleted'] == 1
    assert db.get(Document, identifier).status == DocumentStatus.DELETED


def test_current_original_migration_is_repeatable(accounts, resource_store):
    from hashlib import sha256
    from app.models import KnowledgeBase, Document, DocumentStatus
    from app.migrate_personal_libraries import migrate
    (client, _, _), db = accounts
    register(client)
    actor = access_for(client, db)
    lib = KnowledgeBase(account_id=actor.user_id, owner_id=actor.user_id, name='资料')
    db.add(lib); db.flush()
    def add(key, body, active, **fields):
        doc = Document(knowledge_base_id=lib.id, filename='same.txt', object_key=key,
            size_bytes=len(body), content_hash=sha256(body).hexdigest(), content_type='text/plain',
            status=DocumentStatus.READY, is_active=active, **fields)
        db.add(doc); db.flush()
        resource_store[0][key] = body
        return doc
    old = add('before', b'old', False)
    current = add('after', b'new', True, version_group_id=old.version_group_id, version_number=2)
    db.commit()
    resource_store[0]['after'] = b'bad'
    assert migrate(db, apply=True)['problems'] and lib.directory_path is None
    resource_store[0]['after'] = b'new'
    assert not migrate(db, apply=True)['problems']
    assert current.relative_path == '知识库/资料/same.txt' and current.status == DocumentStatus.READY
    assert old.status == DocumentStatus.DELETED
    assert migrate(db, apply=True)['current'] == []
