# Copyright (C) 2022 Intel Corporation
# Copyright (C) 2022-2023 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import io
import json
import xml.etree.ElementTree as ET
import zipfile
from copy import deepcopy
from http import HTTPStatus
from io import BytesIO
from itertools import product
from time import sleep
from typing import Dict, List, Optional

import pytest
from cvat_sdk.api_client import ApiClient, Configuration, models
from cvat_sdk.api_client.api_client import Endpoint
from cvat_sdk.core.helpers import get_paginated_collection
from deepdiff import DeepDiff
from PIL import Image

from shared.utils.config import (
    BASE_URL,
    USER_PASS,
    get_method,
    make_api_client,
    patch_method,
    post_method,
)

from .utils import CollectionSimpleFilterTestBase, export_dataset


@pytest.mark.usefixtures("restore_db_per_class")
class TestGetProjects:
    def _find_project_by_user_org(self, user, projects, is_project_staff_flag, is_project_staff):
        for p in projects:
            if is_project_staff(user["id"], p["id"]) == is_project_staff_flag:
                return p["id"]

    def _test_response_200(self, username, project_id, **kwargs):
        with make_api_client(username) as api_client:
            (project, response) = api_client.projects_api.retrieve(project_id, **kwargs)
            assert response.status == HTTPStatus.OK
            assert project_id == project.id

    def _test_response_403(self, username, project_id):
        with make_api_client(username) as api_client:
            (_, response) = api_client.projects_api.retrieve(
                project_id, _parse_response=False, _check_status=False
            )
            assert response.status == HTTPStatus.FORBIDDEN

    # Admin can see any project even he has no ownerships for this project.
    def test_project_admin_accessibility(self, projects, find_users, is_project_staff, org_staff):
        users = find_users(privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["organization"])
            and user["id"] not in org_staff(project["organization"])
        )
        self._test_response_200(user["username"], project["id"])

    # Project owner or project assignee can see project.
    def test_project_owner_accessibility(self, projects):
        for p in projects:
            if p["owner"] is not None:
                project_with_owner = p
            if p["assignee"] is not None:
                project_with_assignee = p

        assert project_with_owner is not None
        assert project_with_assignee is not None

        self._test_response_200(project_with_owner["owner"]["username"], project_with_owner["id"])
        self._test_response_200(
            project_with_assignee["assignee"]["username"], project_with_assignee["id"]
        )

    def test_user_cannot_see_project(self, projects, find_users, is_project_staff, org_staff):
        users = find_users(exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["organization"])
            and user["id"] not in org_staff(project["organization"])
        )
        self._test_response_403(user["username"], project["id"])

    @pytest.mark.parametrize("role", ("supervisor", "worker"))
    def test_if_supervisor_or_worker_cannot_see_project(
        self, projects, is_project_staff, find_users, role
    ):
        user, pid = next(
            (
                (user, project["id"])
                for user in find_users(role=role, exclude_privilege="admin")
                for project in projects
                if project["organization"] == user["org"]
                and not is_project_staff(user["id"], project["id"])
            )
        )

        self._test_response_403(user["username"], pid)

    @pytest.mark.parametrize("role", ("maintainer", "owner"))
    def test_if_maintainer_or_owner_can_see_project(
        self, find_users, projects, is_project_staff, role
    ):
        user, pid = next(
            (
                (user, project["id"])
                for user in find_users(role=role, exclude_privilege="admin")
                for project in projects
                if project["organization"] == user["org"]
                and not is_project_staff(user["id"], project["id"])
            )
        )

        self._test_response_200(user["username"], pid, org_id=user["org"])

    @pytest.mark.parametrize("role", ("supervisor", "worker"))
    def test_if_org_member_supervisor_or_worker_can_see_project(
        self, projects, find_users, is_project_staff, role
    ):
        user, pid = next(
            (
                (user, project["id"])
                for user in find_users(role=role, exclude_privilege="admin")
                for project in projects
                if project["organization"] == user["org"]
                and is_project_staff(user["id"], project["id"])
            )
        )

        self._test_response_200(user["username"], pid, org_id=user["org"])


class TestProjectsListFilters(CollectionSimpleFilterTestBase):
    field_lookups = {
        "owner": ["owner", "username"],
        "assignee": ["assignee", "username"],
    }

    @pytest.fixture(autouse=True)
    def setup(self, restore_db_per_class, admin_user, projects):
        self.user = admin_user
        self.samples = projects

    def _get_endpoint(self, api_client: ApiClient) -> Endpoint:
        return api_client.projects_api.list_endpoint

    @pytest.mark.parametrize(
        "field",
        (
            "name",
            "owner",
            "assignee",
            "status",
        ),
    )
    def test_can_use_simple_filter_for_object_list(self, field):
        return super().test_can_use_simple_filter_for_object_list(field)


class TestGetProjectBackup:
    def _test_can_get_project_backup(self, username, pid, **kwargs):
        for _ in range(30):
            response = get_method(username, f"projects/{pid}/backup", **kwargs)
            response.raise_for_status()
            if response.status_code == HTTPStatus.CREATED:
                break
            sleep(1)
        response = get_method(username, f"projects/{pid}/backup", action="download", **kwargs)
        assert response.status_code == HTTPStatus.OK

    def _test_cannot_get_project_backup(self, username, pid, **kwargs):
        response = get_method(username, f"projects/{pid}/backup", **kwargs)
        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_admin_can_get_project_backup(self, projects):
        project = list(projects)[0]
        self._test_can_get_project_backup("admin1", project["id"])

    # User that not in [project:owner, project:assignee] cannot get project backup.
    def test_user_cannot_get_project_backup(self, find_users, projects, is_project_staff):
        users = find_users(exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["id"])
        )

        self._test_cannot_get_project_backup(user["username"], project["id"])

    # Org worker that not in [project:owner, project:assignee] cannot get project backup.
    def test_org_worker_cannot_get_project_backup(
        self, find_users, projects, is_project_staff, is_org_member
    ):
        users = find_users(role="worker", exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
        )

        self._test_cannot_get_project_backup(
            user["username"], project["id"], org_id=project["organization"]
        )

    # Org worker that in [project:owner, project:assignee] can get project backup.
    def test_org_worker_can_get_project_backup(
        self, find_users, projects, is_project_staff, is_org_member
    ):
        users = find_users(role="worker", exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
        )

        self._test_can_get_project_backup(
            user["username"], project["id"], org_id=project["organization"]
        )

    # Org supervisor that in [project:owner, project:assignee] can get project backup.
    def test_org_supervisor_can_get_project_backup(
        self, find_users, projects, is_project_staff, is_org_member
    ):
        users = find_users(role="supervisor", exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
        )

        self._test_can_get_project_backup(
            user["username"], project["id"], org_id=project["organization"]
        )

    # Org supervisor that not in [project:owner, project:assignee] cannot get project backup.
    def test_org_supervisor_cannot_get_project_backup(
        self, find_users, projects, is_project_staff, is_org_member
    ):
        users = find_users(role="supervisor", exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
        )

        self._test_cannot_get_project_backup(
            user["username"], project["id"], org_id=project["organization"]
        )

    # Org maintainer that not in [project:owner, project:assignee] can get project backup.
    def test_org_maintainer_can_get_project_backup(
        self, find_users, projects, is_project_staff, is_org_member
    ):
        users = find_users(role="maintainer", exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
        )

        self._test_can_get_project_backup(
            user["username"], project["id"], org_id=project["organization"]
        )

    # Org owner that not in [project:owner, project:assignee] can get project backup.
    def test_org_owner_can_get_project_backup(
        self, find_users, projects, is_project_staff, is_org_member
    ):
        users = find_users(role="owner", exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
        )

        self._test_can_get_project_backup(
            user["username"], project["id"], org_id=project["organization"]
        )


@pytest.mark.usefixtures("restore_db_per_function")
class TestPostProjects:
    def _test_create_project_201(self, user, spec, **kwargs):
        with make_api_client(user) as api_client:
            (_, response) = api_client.projects_api.create(spec, **kwargs)
            assert response.status == HTTPStatus.CREATED

    def _test_create_project_403(self, user, spec, **kwargs):
        with make_api_client(user) as api_client:
            (_, response) = api_client.projects_api.create(
                spec, **kwargs, _parse_response=False, _check_status=False
            )
        assert response.status == HTTPStatus.FORBIDDEN

    def test_if_worker_cannot_create_project(self, find_users):
        workers = find_users(privilege="worker")
        assert len(workers)

        username = workers[0]["username"]
        spec = {"name": f"test {username} tries to create a project"}
        self._test_create_project_403(username, spec)

    @pytest.mark.parametrize("privilege", ("admin", "business", "user"))
    def test_if_user_can_create_project(self, find_users, privilege):
        privileged_users = find_users(privilege=privilege)
        assert len(privileged_users)

        username = privileged_users[0]["username"]
        spec = {"name": f"test {username} tries to create a project"}
        self._test_create_project_201(username, spec)

    def test_if_org_worker_cannot_create_project(self, find_users):
        workers = find_users(role="worker")

        worker = next(u for u in workers if u["org"])

        spec = {
            "name": f'test: worker {worker["username"]} creating a project for his organization',
        }
        self._test_create_project_403(worker["username"], spec, org_id=worker["org"])

    @pytest.mark.parametrize("role", ("supervisor", "maintainer", "owner"))
    def test_if_org_role_can_create_project(self, role, admin_user):
        # We can hit org or user limits here, so we create a new org and users
        user = self._create_user(
            ApiClient(configuration=Configuration(BASE_URL)), email="test_org_roles@localhost"
        )

        if role != "owner":
            org = self._create_org(make_api_client(admin_user), members={user["email"]: role})
        else:
            org = self._create_org(make_api_client(user["username"]))

        spec = {
            "name": f'test: worker {user["username"]} creating a project for his organization',
        }
        self._test_create_project_201(user["username"], spec, org_id=org)

    @classmethod
    def _create_user(cls, api_client: ApiClient, email: str) -> str:
        username = email.split("@", maxsplit=1)[0]
        with api_client:
            (_, response) = api_client.auth_api.create_register(
                models.RegisterSerializerExRequest(
                    username=username, password1=USER_PASS, password2=USER_PASS, email=email
                )
            )

        api_client.cookies.clear()

        return json.loads(response.data)

    @classmethod
    def _create_org(cls, api_client: ApiClient, members: Optional[Dict[str, str]] = None) -> str:
        with api_client:
            (_, response) = api_client.organizations_api.create(
                models.OrganizationWriteRequest(slug="test_org_roles"), _parse_response=False
            )
            org = json.loads(response.data)["id"]

            for email, role in (members or {}).items():
                api_client.invitations_api.create(
                    models.InvitationWriteRequest(role=role, email=email),
                    org_id=org,
                    _parse_response=False,
                )

        return org

    def test_cannot_create_project_with_same_labels(self, admin_user):
        project_spec = {
            "name": "test cannot create project with same labels",
            "labels": [{"name": "l1"}, {"name": "l1"}],
        }
        response = post_method(admin_user, "/projects", project_spec)
        assert response.status_code == HTTPStatus.BAD_REQUEST

        response = get_method(admin_user, "/projects")
        assert response.status_code == HTTPStatus.OK

    def test_cannot_create_project_with_same_skeleton_sublabels(self, admin_user):
        project_spec = {
            "name": "test cannot create project with same skeleton sublabels",
            "labels": [
                {"name": "s1", "type": "skeleton", "sublabels": [{"name": "1"}, {"name": "1"}]}
            ],
        }
        response = post_method(admin_user, "/projects", project_spec)
        assert response.status_code == HTTPStatus.BAD_REQUEST

        response = get_method(admin_user, "/projects")
        assert response.status_code == HTTPStatus.OK


def _check_cvat_for_video_project_annotations_meta(content, values_to_be_checked):
    document = ET.fromstring(content)
    instance = list(document.find("meta"))[0]
    assert instance.tag == "project"
    assert instance.find("id").text == values_to_be_checked["pid"]
    assert len(list(document.iter("task"))) == len(values_to_be_checked["tasks"])
    tasks = document.iter("task")
    for task_checking in values_to_be_checked["tasks"]:
        task_meta = next(tasks)
        assert task_meta.find("id").text == str(task_checking["id"])
        assert task_meta.find("name").text == task_checking["name"]
        assert task_meta.find("size").text == str(task_checking["size"])
        assert task_meta.find("mode").text == task_checking["mode"]
        assert task_meta.find("source").text


@pytest.mark.usefixtures("restore_db_per_function")
class TestImportExportDatasetProject:
    def _test_export_project(self, username, pid, format_name):
        with make_api_client(username) as api_client:
            return export_dataset(
                api_client.projects_api.retrieve_dataset_endpoint, id=pid, format=format_name
            )

    def _export_annotations(self, username, pid, format_name):
        with make_api_client(username) as api_client:
            return export_dataset(
                api_client.projects_api.retrieve_annotations_endpoint, id=pid, format=format_name
            )

    def _test_import_project(self, username, project_id, format_name, data):
        with make_api_client(username) as api_client:
            (_, response) = api_client.projects_api.create_dataset(
                id=project_id,
                format=format_name,
                dataset_write_request=deepcopy(data),
                _content_type="multipart/form-data",
            )
            assert response.status == HTTPStatus.ACCEPTED

            while True:
                # TODO: It's better be refactored to a separate endpoint to get request status
                (_, response) = api_client.projects_api.retrieve_dataset(
                    project_id, action="import_status"
                )
                if response.status == HTTPStatus.CREATED:
                    break

    def _test_get_annotations_from_task(self, username, task_id):
        with make_api_client(username) as api_client:
            (_, response) = api_client.tasks_api.retrieve_annotations(task_id)
            assert response.status == HTTPStatus.OK

            response_data = json.loads(response.data)
        return response_data

    def test_can_import_dataset_in_org(self, admin_user):
        project_id = 4

        response = self._test_export_project(admin_user, project_id, "CVAT for images 1.1")

        tmp_file = io.BytesIO(response.data)
        tmp_file.name = "dataset.zip"

        import_data = {
            "dataset_file": tmp_file,
        }

        self._test_import_project(admin_user, project_id, "CVAT 1.1", import_data)

    def test_can_export_and_import_dataset_with_skeletons_coco_keypoints(self, admin_user):
        project_id = 5

        response = self._test_export_project(admin_user, project_id, "COCO Keypoints 1.0")

        tmp_file = io.BytesIO(response.data)
        tmp_file.name = "dataset.zip"
        import_data = {
            "dataset_file": tmp_file,
        }

        self._test_import_project(admin_user, project_id, "COCO Keypoints 1.0", import_data)

    def test_can_export_and_import_dataset_with_skeletons_cvat_for_images(self, admin_user):
        project_id = 5

        response = self._test_export_project(admin_user, project_id, "CVAT for images 1.1")

        tmp_file = io.BytesIO(response.data)
        tmp_file.name = "dataset.zip"
        import_data = {
            "dataset_file": tmp_file,
        }

        self._test_import_project(admin_user, project_id, "CVAT 1.1", import_data)

    def test_can_export_and_import_dataset_with_skeletons_cvat_for_video(self, admin_user):
        project_id = 5

        response = self._test_export_project(admin_user, project_id, "CVAT for video 1.1")

        tmp_file = io.BytesIO(response.data)
        tmp_file.name = "dataset.zip"
        import_data = {
            "dataset_file": tmp_file,
        }

        self._test_import_project(admin_user, project_id, "CVAT 1.1", import_data)

    def _test_can_get_project_backup(self, username, pid, **kwargs):
        for _ in range(30):
            response = get_method(username, f"projects/{pid}/backup", **kwargs)
            response.raise_for_status()
            if response.status_code == HTTPStatus.CREATED:
                break
            sleep(1)
        response = get_method(username, f"projects/{pid}/backup", action="download", **kwargs)
        assert response.status_code == HTTPStatus.OK
        return response

    def test_admin_can_get_project_backup_and_create_project_by_backup(self, admin_user):
        project_id = 5
        response = self._test_can_get_project_backup(admin_user, project_id)

        tmp_file = io.BytesIO(response.content)
        tmp_file.name = "dataset.zip"

        import_data = {
            "project_file": tmp_file,
        }

        with make_api_client(admin_user) as api_client:
            (_, response) = api_client.projects_api.create_backup(
                backup_write_request=deepcopy(import_data), _content_type="multipart/form-data"
            )
            assert response.status == HTTPStatus.ACCEPTED

    @pytest.mark.parametrize("format_name", ("Datumaro 1.0", "ImageNet 1.0", "PASCAL VOC 1.1"))
    def test_can_import_export_dataset_with_some_format(self, format_name):
        # https://github.com/opencv/cvat/issues/4410
        # https://github.com/opencv/cvat/issues/4850
        # https://github.com/opencv/cvat/issues/4621
        username = "admin1"
        project_id = 4

        response = self._test_export_project(username, project_id, format_name)

        tmp_file = io.BytesIO(response.data)
        tmp_file.name = "dataset.zip"

        import_data = {
            "dataset_file": tmp_file,
        }

        self._test_import_project(username, project_id, format_name, import_data)

    @pytest.mark.parametrize("username, pid", [("admin1", 8)])
    @pytest.mark.parametrize(
        "anno_format, anno_file_name, check_func",
        [
            (
                "CVAT for video 1.1",
                "annotations.xml",
                _check_cvat_for_video_project_annotations_meta,
            ),
        ],
    )
    def test_exported_project_dataset_structure(
        self,
        username,
        pid,
        anno_format,
        anno_file_name,
        check_func,
        tasks,
        projects,
        annotations,
    ):
        project = projects[pid]

        values_to_be_checked = {
            "pid": str(pid),
            "name": project["name"],
            "tasks": [
                {
                    "id": task["id"],
                    "name": task["name"],
                    "size": str(task["size"]),
                    "mode": task["mode"],
                }
                for task in tasks
                if task["project_id"] == project["id"]
            ],
        }

        response = self._export_annotations(username, pid, anno_format)
        assert response.data
        with zipfile.ZipFile(BytesIO(response.data)) as zip_file:
            content = zip_file.read(anno_file_name)
        check_func(content, values_to_be_checked)

    def test_can_import_export_annotations_with_rotation(self):
        # https://github.com/opencv/cvat/issues/4378
        username = "admin1"
        project_id = 4

        response = self._test_export_project(username, project_id, "CVAT for images 1.1")

        tmp_file = io.BytesIO(response.data)
        tmp_file.name = "dataset.zip"

        import_data = {
            "dataset_file": tmp_file,
        }

        self._test_import_project(username, project_id, "CVAT 1.1", import_data)

        response = get_method(username, f"/tasks", project_id=project_id)
        assert response.status_code == HTTPStatus.OK
        tasks = response.json()["results"]

        response_data = self._test_get_annotations_from_task(username, tasks[0]["id"])
        task1_rotation = response_data["shapes"][0]["rotation"]
        response_data = self._test_get_annotations_from_task(username, tasks[1]["id"])
        task2_rotation = response_data["shapes"][0]["rotation"]

        assert task1_rotation == task2_rotation


@pytest.mark.usefixtures("restore_db_per_function")
class TestPatchProjectLabel:
    def _get_project_labels(self, pid, user, **kwargs) -> List[models.Label]:
        kwargs.setdefault("return_json", True)
        with make_api_client(user) as api_client:
            return get_paginated_collection(
                api_client.labels_api.list_endpoint, project_id=pid, **kwargs
            )

    def test_can_delete_label(self, projects, labels, admin_user):
        project = [p for p in projects if p["labels"]["count"] > 0][0]
        label = deepcopy([l for l in labels if l.get("project_id") == project["id"]][0])
        label_payload = {"id": label["id"], "deleted": True}

        response = patch_method(
            admin_user, f'/projects/{project["id"]}', {"labels": [label_payload]}
        )
        assert response.status_code == HTTPStatus.OK, response.content
        assert response.json()["labels"]["count"] == project["labels"]["count"] - 1

    def test_can_delete_skeleton_label(self, projects, labels, admin_user):
        project = next(
            p
            for p in projects
            if any(
                label
                for label in labels
                if label.get("project_id") == p["id"]
                if label["type"] == "skeleton"
            )
        )
        project_labels = deepcopy([l for l in labels if l.get("project_id") == project["id"]])
        label = next(l for l in project_labels if l["type"] == "skeleton")
        project_labels.remove(label)
        label_payload = {"id": label["id"], "deleted": True}

        response = patch_method(
            admin_user, f'/projects/{project["id"]}', {"labels": [label_payload]}
        )
        assert response.status_code == HTTPStatus.OK
        assert response.json()["labels"]["count"] == project["labels"]["count"] - 1

        resulting_labels = self._get_project_labels(project["id"], admin_user)
        assert DeepDiff(resulting_labels, project_labels, ignore_order=True) == {}

    def test_can_rename_label(self, projects, labels, admin_user):
        project = [p for p in projects if p["labels"]["count"] > 0][0]
        project_labels = deepcopy([l for l in labels if l.get("project_id") == project["id"]])
        project_labels[0].update({"name": "new name"})

        response = patch_method(
            admin_user, f'/projects/{project["id"]}', {"labels": [project_labels[0]]}
        )
        assert response.status_code == HTTPStatus.OK

        resulting_labels = self._get_project_labels(project["id"], admin_user)
        assert DeepDiff(resulting_labels, project_labels, ignore_order=True) == {}

    def test_cannot_rename_label_to_duplicate_name(self, projects, labels, admin_user):
        project = [p for p in projects if p["labels"]["count"] > 1][0]
        project_labels = deepcopy([l for l in labels if l.get("project_id") == project["id"]])
        project_labels[0].update({"name": project_labels[1]["name"]})

        label_payload = {"id": project_labels[0]["id"], "name": project_labels[0]["name"]}

        response = patch_method(
            admin_user, f'/projects/{project["id"]}', {"labels": [label_payload]}
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert "All label names must be unique" in response.text

    def test_cannot_add_foreign_label(self, projects, labels, admin_user):
        project = list(projects)[0]
        new_label = deepcopy([l for l in labels if l.get("project_id") != project["id"]][0])

        response = patch_method(admin_user, f'/projects/{project["id"]}', {"labels": [new_label]})
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert f"Not found label with id #{new_label['id']} to change" in response.text

    def test_admin_can_add_label(self, projects, admin_user):
        project = list(projects)[0]
        new_label = {"name": "new name"}

        response = patch_method(admin_user, f'/projects/{project["id"]}', {"labels": [new_label]})
        assert response.status_code == HTTPStatus.OK
        assert response.json()["labels"]["count"] == project["labels"]["count"] + 1

    @pytest.mark.parametrize("role", ["maintainer", "owner"])
    def test_non_project_staff_privileged_org_members_can_add_label(
        self,
        find_users,
        projects,
        is_project_staff,
        is_org_member,
        role,
    ):
        users = find_users(role=role, exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
        )

        new_label = {"name": "new name"}
        response = patch_method(
            user["username"],
            f'/projects/{project["id"]}',
            {"labels": [new_label]},
            org_id=project["organization"],
        )
        assert response.status_code == HTTPStatus.OK
        assert response.json()["labels"]["count"] == project["labels"]["count"] + 1

    @pytest.mark.parametrize("role", ["supervisor", "worker"])
    def test_non_project_staff_org_members_cannot_add_label(
        self,
        find_users,
        projects,
        is_project_staff,
        is_org_member,
        role,
    ):
        users = find_users(role=role, exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
        )

        new_label = {"name": "new name"}
        response = patch_method(
            user["username"],
            f'/projects/{project["id"]}',
            {"labels": [new_label]},
            org_id=project["organization"],
        )
        assert response.status_code == HTTPStatus.FORBIDDEN

    # TODO: add supervisor too, but this leads to a test-side problem with DB restoring
    @pytest.mark.parametrize("role", ["worker"])
    def test_project_staff_org_members_can_add_label(
        self, find_users, projects, is_project_staff, is_org_member, labels, role
    ):
        users = find_users(role=role, exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if is_project_staff(user["id"], project["id"])
            and project["organization"]
            and is_org_member(user["id"], project["organization"])
            and any(label.get("project_id") == project["id"] for label in labels)
        )

        new_label = {"name": "new name"}
        response = patch_method(
            user["username"],
            f'/projects/{project["id"]}',
            {"labels": [new_label]},
            org_id=project["organization"],
        )
        assert response.status_code == HTTPStatus.OK
        assert response.json()["labels"]["count"] == project["labels"]["count"] + 1


@pytest.mark.usefixtures("restore_db_per_class")
class TestGetProjectPreview:
    def _test_response_200(self, username, project_id, **kwargs):
        with make_api_client(username) as api_client:
            (_, response) = api_client.projects_api.retrieve_preview(project_id, **kwargs)

            assert response.status == HTTPStatus.OK
            (width, height) = Image.open(BytesIO(response.data)).size
            assert width > 0 and height > 0

    def _test_response_403(self, username, project_id):
        with make_api_client(username) as api_client:
            (_, response) = api_client.projects_api.retrieve_preview(
                project_id, _parse_response=False, _check_status=False
            )
            assert response.status == HTTPStatus.FORBIDDEN

    def _test_response_404(self, username, project_id):
        with make_api_client(username) as api_client:
            (_, response) = api_client.projects_api.retrieve_preview(
                project_id, _parse_response=False, _check_status=False
            )
            assert response.status == HTTPStatus.NOT_FOUND

    # Admin can see any project preview even he has no ownerships for this project.
    def test_project_preview_admin_accessibility(
        self, projects, find_users, is_project_staff, org_staff
    ):
        users = find_users(privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["organization"])
            and user["id"] not in org_staff(project["organization"])
            and project["tasks"]["count"] > 0
        )
        self._test_response_200(user["username"], project["id"])

    # Project owner or project assignee can see project preview.
    def test_project_preview_owner_accessibility(self, projects):
        for p in projects:
            if not p["tasks"]:
                continue
            if p["owner"] is not None:
                project_with_owner = p
            if p["assignee"] is not None:
                project_with_assignee = p

        assert project_with_owner is not None
        assert project_with_assignee is not None

        self._test_response_200(project_with_owner["owner"]["username"], project_with_owner["id"])
        self._test_response_200(
            project_with_assignee["assignee"]["username"], project_with_assignee["id"]
        )

    def test_project_preview_not_found(self, projects, tasks):
        for p in projects:
            if any(t["project_id"] == p["id"] for t in tasks):
                continue
            if p["owner"] is not None:
                project_with_owner = p
            if p["assignee"] is not None:
                project_with_assignee = p

        assert project_with_owner is not None
        assert project_with_assignee is not None

        self._test_response_404(project_with_owner["owner"]["username"], project_with_owner["id"])
        self._test_response_404(
            project_with_assignee["assignee"]["username"], project_with_assignee["id"]
        )

    def test_user_cannot_see_project_preview(
        self, projects, find_users, is_project_staff, org_staff
    ):
        users = find_users(exclude_privilege="admin")

        user, project = next(
            (user, project)
            for user, project in product(users, projects)
            if not is_project_staff(user["id"], project["organization"])
            and user["id"] not in org_staff(project["organization"])
        )
        self._test_response_403(user["username"], project["id"])

    @pytest.mark.parametrize("role", ("supervisor", "worker"))
    def test_if_supervisor_or_worker_cannot_see_project_preview(
        self, projects, is_project_staff, find_users, role
    ):
        user, pid = next(
            (
                (user, project["id"])
                for user in find_users(role=role, exclude_privilege="admin")
                for project in projects
                if project["organization"] == user["org"]
                and not is_project_staff(user["id"], project["id"])
            )
        )

        self._test_response_403(user["username"], pid)

    @pytest.mark.parametrize("role", ("maintainer", "owner"))
    def test_if_maintainer_or_owner_can_see_project_preview(
        self, find_users, projects, is_project_staff, role
    ):
        user, pid = next(
            (
                (user, project["id"])
                for user in find_users(role=role, exclude_privilege="admin")
                for project in projects
                if project["organization"] == user["org"]
                and not is_project_staff(user["id"], project["id"])
                and project["tasks"]["count"] > 0
            )
        )

        self._test_response_200(user["username"], pid, org_id=user["org"])
