# Copyright (C) 2019-2022 Intel Corporation
# Copyright (C) 2022-2023 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from copy import copy
from inspect import isclass
import os
import re
import shutil

from tempfile import NamedTemporaryFile
import textwrap
from typing import Any, Dict, Iterable, Optional, OrderedDict, Union

from rest_framework import serializers, exceptions
from django.contrib.auth.models import User, Group
from django.db import transaction

from cvat.apps.dataset_manager.formats.utils import get_label_color
from cvat.apps.engine import models
from cvat.apps.engine.cloud_provider import get_cloud_storage_instance, Credentials, Status
from cvat.apps.engine.log import slogger
from cvat.apps.engine.utils import parse_specific_attributes

from drf_spectacular.utils import OpenApiExample, extend_schema_field, extend_schema_serializer

from cvat.apps.engine.view_utils import build_field_filter_params, get_list_view_name, reverse

@extend_schema_field(serializers.URLField)
class HyperlinkedEndpointSerializer(serializers.Serializer):
    key_field = 'pk'

    def __init__(self, view_name=None, *, filter_key=None, **kwargs):
        if isclass(view_name) and issubclass(view_name, models.models.Model):
            view_name = get_list_view_name(view_name)
        elif not isinstance(view_name, str):
            raise TypeError(view_name)

        kwargs['read_only'] = True
        super().__init__(**kwargs)

        self.view_name = view_name
        self.filter_key = filter_key

    def get_attribute(self, instance):
        return instance

    def to_representation(self, instance):
        request = self.context.get('request')
        if not request:
            return None

        return serializers.Hyperlink(
            reverse(self.view_name, request=request,
                query_params=build_field_filter_params(
                    self.filter_key, getattr(instance, self.key_field)
            )),
            instance
        )


class _CollectionSummarySerializer(serializers.Serializer):
    # This class isn't recommended for direct use in public serializers
    # because it produces too generic description in the schema.
    # Consider creating a dedicated inherited class instead.

    count = serializers.IntegerField(default=0)

    def __init__(self, model, *, url_filter_key, **kwargs):
        super().__init__(**kwargs)
        self._collection_key = self.source
        self._model = model
        self._url_filter_key = url_filter_key

    def bind(self, field_name, parent):
        super().bind(field_name, parent)
        self._collection_key = self._collection_key or self.source
        self._model = self._model or type(self.parent)

    def get_fields(self):
        fields = super().get_fields()
        fields['url'] = HyperlinkedEndpointSerializer(self._model, filter_key=self._url_filter_key)
        fields['count'].source = self._collection_key + '.count'
        return fields

    def get_attribute(self, instance):
        return instance


class LabelsSummarySerializer(_CollectionSummarySerializer):
    def __init__(self, *, model=models.Label, url_filter_key, source='get_labels', **kwargs):
        super().__init__(model=model, url_filter_key=url_filter_key, source=source, **kwargs)


class JobsSummarySerializer(_CollectionSummarySerializer):
    completed = serializers.IntegerField(source='completed_jobs_count', default=0)

    def __init__(self, *, model=models.Job, url_filter_key, **kwargs):
        super().__init__(model=model, url_filter_key=url_filter_key, **kwargs)


class TasksSummarySerializer(_CollectionSummarySerializer):
    pass


class CommentsSummarySerializer(_CollectionSummarySerializer):
    pass


class IssuesSummarySerializer(_CollectionSummarySerializer):
    pass


class BasicUserSerializer(serializers.ModelSerializer):
    def validate(self, attrs):
        if hasattr(self, 'initial_data'):
            unknown_keys = set(self.initial_data.keys()) - set(self.fields.keys())
            if unknown_keys:
                if set(['is_staff', 'is_superuser', 'groups']) & unknown_keys:
                    message = 'You do not have permissions to access some of' + \
                        ' these fields: {}'.format(unknown_keys)
                else:
                    message = 'Got unknown fields: {}'.format(unknown_keys)
                raise serializers.ValidationError(message)
        return attrs

    class Meta:
        model = User
        fields = ('url', 'id', 'username', 'first_name', 'last_name')

class UserSerializer(serializers.ModelSerializer):
    groups = serializers.SlugRelatedField(many=True,
        slug_field='name', queryset=Group.objects.all())

    class Meta:
        model = User
        fields = ('url', 'id', 'username', 'first_name', 'last_name', 'email',
            'groups', 'is_staff', 'is_superuser', 'is_active', 'last_login',
            'date_joined')
        read_only_fields = ('last_login', 'date_joined')
        write_only_fields = ('password', )
        extra_kwargs = {
            'last_login': { 'allow_null': True }
        }

class AttributeSerializer(serializers.ModelSerializer):
    values = serializers.ListField(allow_empty=True,
        child=serializers.CharField(max_length=200),
    )

    class Meta:
        model = models.AttributeSpec
        fields = ('id', 'name', 'mutable', 'input_type', 'default_value', 'values')

    # pylint: disable=no-self-use
    def to_internal_value(self, data):
        attribute = data.copy()
        attribute['values'] = '\n'.join(data.get('values', []))
        return attribute

    def to_representation(self, instance):
        if instance:
            rep = super().to_representation(instance)
            rep['values'] = instance.values.split('\n')
        else:
            rep = instance

        return rep

class SublabelSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    attributes = AttributeSerializer(many=True, source='attributespec_set', default=[],
        help_text="The list of attributes. "
        "If you want to remove an attribute, you need to recreate the label "
        "and specify the remaining attributes.")
    color = serializers.CharField(allow_blank=True, required=False,
        help_text="The hex value for the RGB color. "
        "Will be generated automatically, unless specified explicitly.")
    type = serializers.CharField(allow_blank=True, required=False,
        help_text="Associated annotation type for this label")
    has_parent = serializers.BooleanField(source='has_parent_label', required=False)

    class Meta:
        model = models.Label
        fields = ('id', 'name', 'color', 'attributes', 'type', 'has_parent', )
        read_only_fields = ('parent',)

class SkeletonSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    svg = serializers.CharField(allow_blank=True, required=False)

    class Meta:
        model = models.Skeleton
        fields = ('id', 'svg',)

class LabelSerializer(SublabelSerializer):
    deleted = serializers.BooleanField(required=False, write_only=True,
        help_text='Delete the label. Only applicable in the PATCH methods of a project or a task.')
    sublabels = SublabelSerializer(many=True, required=False)
    svg = serializers.CharField(allow_blank=True, required=False)
    has_parent = serializers.BooleanField(read_only=True, source='has_parent_label', required=False)

    class Meta:
        model = models.Label
        fields = (
            'id', 'name', 'color', 'attributes', 'deleted', 'type', 'svg',
            'sublabels', 'project_id', 'task_id', 'parent_id', 'has_parent'
        )
        read_only_fields = ('id', 'svg', 'project_id', 'task_id')
        extra_kwargs = {
            'project_id': { 'required': False, 'allow_null': False },
            'task_id': { 'required': False, 'allow_null': False },
            'parent_id': { 'required': False, },
        }

    def to_representation(self, instance):
        label = super().to_representation(instance)
        if label['type'] == str(models.LabelType.SKELETON):
            label['svg'] = instance.skeleton.svg

        # Clean mutually exclusive fields
        if not label.get('task_id'):
            label.pop('task_id', None)
        if not label.get('project_id'):
            label.pop('project_id', None)

        return label

    def __init__(self, *args, **kwargs):
        self._local = kwargs.pop('local', False)
        """
        Indicates that the operation is called from the dedicated ViewSet
        and not from the parent entity, i.e. a project or task.
        """

        super().__init__(*args, **kwargs)

    def validate(self, attrs):
        if self._local and attrs.get('deleted'):
            # NOTE: Navigate clients to the right method
            raise serializers.ValidationError(
                'Labels cannot be deleted by updating in this endpoint. '
                'Please use the DELETE method instead.'
            )

        if attrs.get('deleted') and attrs.get('id') is None:
            raise serializers.ValidationError('Deleted label must have an ID')

        return attrs

    @classmethod
    @transaction.atomic
    def update_label(
        cls,
        validated_data: Dict[str, Any],
        *,
        parent_instance: Union[models.Project, models.Task],
        parent_label: Optional[models.Label] = None
    ) -> Optional[models.Label]:
        parent_info, logger = cls._get_parent_info(parent_instance)

        attributes = validated_data.pop('attributespec_set', [])

        if validated_data.get('id') is not None:
            try:
                db_label = models.Label.objects.get(id=validated_data['id'], **parent_info)
            except models.Label.DoesNotExist as exc:
                raise exceptions.NotFound(
                    detail='Not found label with id #{} to change'.format(validated_data['id'])
                ) from exc

            updated_type = validated_data.get('type') or db_label.type
            if str(models.LabelType.SKELETON) in [db_label.type, updated_type]:
                # do not permit changing types from/to skeleton
                logger.warning("Label id {} ({}): an attempt to change label type from {} to {}. "
                    "Changing from or to '{}' is not allowed, the type won't be changed.".format(
                    db_label.id,
                    db_label.name,
                    db_label.type,
                    updated_type,
                    str(models.LabelType.SKELETON),
                ))
            else:
                db_label.type = updated_type

            db_label.name = validated_data.get('name') or db_label.name

            logger.info("Label id {} ({}) was updated".format(db_label.id, db_label.name))
        else:
            try:
                db_label = models.Label.create(
                    name=validated_data.get('name'),
                    type=validated_data.get('type'),
                    parent=parent_label,
                    **parent_info
                )
            except models.InvalidLabel as exc:
                raise exceptions.ValidationError(str(exc)) from exc
            logger.info("New {} label was created".format(db_label.name))

        if validated_data.get('deleted'):
            assert validated_data['id'] # must be checked in the validate()
            db_label.delete()
            return None

        if not validated_data.get('color', None):
            other_label_colors = [
                label.color for label in
                parent_instance.label_set.exclude(id=db_label.id).order_by('id')
            ]
            db_label.color = get_label_color(db_label.name, other_label_colors)
        else:
            db_label.color = validated_data.get('color', db_label.color)

        try:
            db_label.save()
        except models.InvalidLabel as exc:
            raise exceptions.ValidationError(str(exc)) from exc

        for attr in attributes:
            (db_attr, created) = models.AttributeSpec.objects.get_or_create(
                label=db_label, name=attr['name'], defaults=attr
            )
            if created:
                logger.info("New {} attribute for {} label was created"
                    .format(db_attr.name, db_label.name))
            else:
                logger.info("{} attribute for {} label was updated"
                    .format(db_attr.name, db_label.name))

                # FIXME: need to update only "safe" fields
                db_attr.default_value = attr.get('default_value', db_attr.default_value)
                db_attr.mutable = attr.get('mutable', db_attr.mutable)
                db_attr.input_type = attr.get('input_type', db_attr.input_type)
                db_attr.values = attr.get('values', db_attr.values)
                db_attr.save()

        return db_label

    @classmethod
    @transaction.atomic
    def create_labels(cls,
        labels: Iterable[Dict[str, Any]],
        *,
        parent_instance: Union[models.Project, models.Task],
        parent_label: Optional[models.Label] = None
    ):
        parent_info, logger = cls._get_parent_info(parent_instance)

        label_colors = list()

        for label in labels:
            attributes = label.pop('attributespec_set')

            if label.get('id', None):
                del label['id']

            if not label.get('color', None):
                label['color'] = get_label_color(label['name'], label_colors)
            label_colors.append(label['color'])

            sublabels = label.pop('sublabels', [])
            svg = label.pop('svg', '')
            try:
                db_label = models.Label.create(**label, **parent_info, parent=parent_label)
            except models.InvalidLabel as exc:
                raise exceptions.ValidationError(str(exc)) from exc
            logger.info(
                f'label:create Label id:{db_label.id} for spec:{label} '
                f'with sublabels:{sublabels}, parent_label:{parent_label}'
            )

            cls.create_labels(sublabels, parent_instance=parent_instance, parent_label=db_label)

            if db_label.type == str(models.LabelType.SKELETON):
                for db_sublabel in list(db_label.sublabels.all()):
                    svg = svg.replace(
                        f'data-label-name="{db_sublabel.name}"',
                        f'data-label-id="{db_sublabel.id}"'
                    )
                db_skeleton = models.Skeleton.objects.create(root=db_label, svg=svg)
                logger.info(f'label:create Skeleton id:{db_skeleton.id} for label_id:{db_label.id}')

            for attr in attributes:
                if attr.get('id', None):
                    del attr['id']
                models.AttributeSpec.objects.create(label=db_label, **attr)

    @classmethod
    @transaction.atomic
    def update_labels(cls,
        labels: Iterable[Dict[str, Any]],
        *,
        parent_instance: Union[models.Project, models.Task],
        parent_label: Optional[models.Label] = None
    ):
        _, logger = cls._get_parent_info(parent_instance)

        for label in labels:
            sublabels = label.pop('sublabels', [])
            svg = label.pop('svg', '')
            db_label = cls.update_label(label,
                parent_instance=parent_instance, parent_label=parent_label
            )
            if db_label:
                logger.info(
                    f'label:update Label id:{db_label.id} for spec:{label} '
                    f'with sublabels:{sublabels}, parent_label:{parent_label}'
                )
            else:
                logger.info(
                    f'label:delete label:{label} with '
                    f'sublabels:{sublabels}, parent_label:{parent_label}'
                )

            if not label.get('deleted'):
                cls.update_labels(sublabels, parent_instance=parent_instance, parent_label=db_label)

                if label.get('id') is None and db_label.type == str(models.LabelType.SKELETON):
                    for db_sublabel in list(db_label.sublabels.all()):
                        svg = svg.replace(
                            f'data-label-name="{db_sublabel.name}"',
                            f'data-label-id="{db_sublabel.id}"'
                        )
                    db_skeleton = models.Skeleton.objects.create(root=db_label, svg=svg)
                    logger.info(
                        f'label:update Skeleton id:{db_skeleton.id} for label_id:{db_label.id}'
                    )

    @classmethod
    def _get_parent_info(cls, parent_instance: Union[models.Project, models.Task]):
        parent_info = {}
        if isinstance(parent_instance, models.Project):
            parent_info['project'] = parent_instance
            logger = slogger.project[parent_instance.id]
        elif isinstance(parent_instance, models.Task):
            parent_info['task'] = parent_instance
            logger = slogger.task[parent_instance.id]
        else:
            raise TypeError(f"Unexpected parent instance type {type(parent_instance).__name__}")

        return parent_info, logger

    def update(self, instance, validated_data):
        if not self._local:
            return super().update(instance, validated_data)

        # Here we reuse the parent entity logic to make sure everything is done
        # like these entities expect. Initial data (unprocessed) is used to
        # avoid introducing premature changes.
        data = copy(self.initial_data)
        data['id'] = instance.id
        data.setdefault('name', instance.name)
        parent_query = { 'labels': [data] }

        if isinstance(instance.project, models.Project):
            parent_serializer = ProjectWriteSerializer(
                instance=instance.project, data=parent_query, partial=True,
            )
        elif isinstance(instance.task, models.Task):
            parent_serializer = TaskWriteSerializer(
                instance=instance.task, data=parent_query, partial=True,
            )

        parent_serializer.is_valid(raise_exception=True)
        parent_serializer.save()

        self.instance = models.Label.objects.get(pk=instance.pk)
        return self.instance


class JobReadSerializer(serializers.ModelSerializer):
    task_id = serializers.ReadOnlyField(source="segment.task.id")
    project_id = serializers.ReadOnlyField(source="get_project_id", allow_null=True)
    start_frame = serializers.ReadOnlyField(source="segment.start_frame")
    stop_frame = serializers.ReadOnlyField(source="segment.stop_frame")
    assignee = BasicUserSerializer(allow_null=True, read_only=True)
    dimension = serializers.CharField(max_length=2, source='segment.task.dimension', read_only=True)
    data_chunk_size = serializers.ReadOnlyField(source='segment.task.data.chunk_size')
    data_compressed_chunk_type = serializers.ReadOnlyField(source='segment.task.data.compressed_chunk_type')
    mode = serializers.ReadOnlyField(source='segment.task.mode')
    bug_tracker = serializers.CharField(max_length=2000, source='get_bug_tracker',
        allow_null=True, read_only=True)
    labels = LabelsSummarySerializer(url_filter_key='job_id')
    issues = IssuesSummarySerializer(models.Issue, url_filter_key='job_id')

    class Meta:
        model = models.Job
        fields = ('url', 'id', 'task_id', 'project_id', 'assignee',
            'dimension', 'bug_tracker', 'status', 'stage', 'state', 'mode',
            'start_frame', 'stop_frame', 'data_chunk_size', 'data_compressed_chunk_type',
            'updated_date', 'issues', 'labels')
        read_only_fields = fields

class JobWriteSerializer(serializers.ModelSerializer):
    assignee = serializers.IntegerField(allow_null=True, required=False)

    def to_representation(self, instance):
        # FIXME: deal with resquest/response separation
        serializer = JobReadSerializer(instance, context=self.context)
        return serializer.data

    def update(self, instance, validated_data):
        state = validated_data.get('state')
        stage = validated_data.get('stage')
        if stage:
            if stage == models.StageChoice.ANNOTATION:
                status = models.StatusChoice.ANNOTATION
            elif stage == models.StageChoice.ACCEPTANCE and state == models.StateChoice.COMPLETED:
                status = models.StatusChoice.COMPLETED
            else:
                status = models.StatusChoice.VALIDATION

            validated_data['status'] = status
            if stage != instance.stage and not state:
                validated_data['state'] = models.StateChoice.NEW

        assignee = validated_data.get('assignee')
        if assignee is not None:
            validated_data['assignee'] = User.objects.get(id=assignee)

        instance = super().update(instance, validated_data)

        return instance


    class Meta:
        model = models.Job
        fields = ('assignee', 'stage', 'state')

class SimpleJobSerializer(serializers.ModelSerializer):
    assignee = BasicUserSerializer(allow_null=True)

    class Meta:
        model = models.Job
        fields = ('url', 'id', 'assignee', 'status', 'stage', 'state')
        read_only_fields = fields

class SegmentSerializer(serializers.ModelSerializer):
    jobs = SimpleJobSerializer(many=True, source='job_set')

    class Meta:
        model = models.Segment
        fields = ('start_frame', 'stop_frame', 'jobs')
        read_only_fields = fields

class ClientFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ClientFile
        fields = ('file', )

    # pylint: disable=no-self-use
    def to_internal_value(self, data):
        return {'file': data}

    # pylint: disable=no-self-use
    def to_representation(self, instance):
        if instance:
            upload_dir = instance.data.get_upload_dirname()
            return instance.file.path[len(upload_dir) + 1:]
        else:
            return instance

class ServerFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ServerFile
        fields = ('file', )

    # pylint: disable=no-self-use
    def to_internal_value(self, data):
        return {'file': data}

    # pylint: disable=no-self-use
    def to_representation(self, instance):
        return instance.file if instance else instance

class RemoteFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.RemoteFile
        fields = ('file', )

    # pylint: disable=no-self-use
    def to_internal_value(self, data):
        return {'file': data}

    # pylint: disable=no-self-use
    def to_representation(self, instance):
        return instance.file if instance else instance

class RqStatusSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=[
        "Queued", "Started", "Finished", "Failed"])
    message = serializers.CharField(allow_blank=True, default="")
    progress = serializers.FloatField(max_value=100, default=0)

class WriteOnceMixin:
    """
    Adds support for write once fields to serializers.

    To use it, specify a list of fields as `write_once_fields` on the
    serializer's Meta:
    ```
    class Meta:
        model = SomeModel
        fields = '__all__'
        write_once_fields = ('collection', )
    ```

    Now the fields in `write_once_fields` can be set during POST (create),
    but cannot be changed afterwards via PUT or PATCH (update).
    Inspired by http://stackoverflow.com/a/37487134/627411.
    """

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()

        # We're only interested in PATCH/PUT.
        if 'update' in getattr(self.context.get('view'), 'action', ''):
            extra_kwargs = self._set_write_once_fields(extra_kwargs)

        return extra_kwargs

    def _set_write_once_fields(self, extra_kwargs):
        """
        Set all fields in `Meta.write_once_fields` to read_only.
        """

        write_once_fields = getattr(self.Meta, 'write_once_fields', None)
        if not write_once_fields:
            return extra_kwargs

        if not isinstance(write_once_fields, (list, tuple)):
            raise TypeError(
                'The `write_once_fields` option must be a list or tuple. '
                'Got {}.'.format(type(write_once_fields).__name__)
            )

        for field_name in write_once_fields:
            kwargs = extra_kwargs.get(field_name, {})
            kwargs['read_only'] = True
            extra_kwargs[field_name] = kwargs

        return extra_kwargs


class JobFiles(serializers.ListField):
    """
    Read JobFileMapping docs for more info.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('child', serializers.CharField(allow_blank=False, max_length=1024))
        kwargs.setdefault('allow_empty', False)
        super().__init__(*args, **kwargs)


class JobFileMapping(serializers.ListField):
    """
    Represents a file-to-job mapping. Useful to specify a custom job
    configuration during task creation. This option is not compatible with
    most other job split-related options.

    Example:
    [
        ["file1.jpg", "file2.jpg"], # job #1 files
        ["file3.png"], # job #2 files
        ["file4.jpg", "file5.png", "file6.bmp"], # job #3 files
    ]

    Files in the jobs must not overlap and repeat.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('child', JobFiles())
        kwargs.setdefault('allow_empty', False)
        kwargs.setdefault('help_text', textwrap.dedent(__class__.__doc__))
        super().__init__(*args, **kwargs)


class DataSerializer(WriteOnceMixin, serializers.ModelSerializer):
    image_quality = serializers.IntegerField(min_value=0, max_value=100)
    use_zip_chunks = serializers.BooleanField(default=False)
    client_files = ClientFileSerializer(many=True, default=[])
    server_files = ServerFileSerializer(many=True, default=[])
    remote_files = RemoteFileSerializer(many=True, default=[])
    use_cache = serializers.BooleanField(default=False)
    copy_data = serializers.BooleanField(default=False)
    cloud_storage_id = serializers.IntegerField(write_only=True, allow_null=True, required=False)
    filename_pattern = serializers.CharField(allow_null=True, required=False)
    job_file_mapping = JobFileMapping(required=False, write_only=True)

    class Meta:
        model = models.Data
        fields = ('chunk_size', 'size', 'image_quality', 'start_frame', 'stop_frame', 'frame_filter',
            'compressed_chunk_type', 'original_chunk_type', 'client_files', 'server_files', 'remote_files', 'use_zip_chunks',
            'cloud_storage_id', 'use_cache', 'copy_data', 'storage_method', 'storage', 'sorting_method', 'filename_pattern',
            'job_file_mapping')

    # pylint: disable=no-self-use
    def validate_frame_filter(self, value):
        match = re.search(r"step\s*=\s*([1-9]\d*)", value)
        if not match:
            raise serializers.ValidationError("Invalid frame filter expression")
        return value

    # pylint: disable=no-self-use
    def validate_chunk_size(self, value):
        if not value > 0:
            raise serializers.ValidationError('Chunk size must be a positive integer')
        return value

    def validate_job_file_mapping(self, value):
        existing_files = set()

        for job_files in value:
            for filename in job_files:
                if filename in existing_files:
                    raise serializers.ValidationError(
                        f"The same file '{filename}' cannot be used multiple "
                        "times in the job file mapping"
                    )

                existing_files.add(filename)

        return value

    # pylint: disable=no-self-use
    def validate(self, attrs):
        if 'start_frame' in attrs and 'stop_frame' in attrs \
            and attrs['start_frame'] > attrs['stop_frame']:
            raise serializers.ValidationError('Stop frame must be more or equal start frame')

        return attrs

    def create(self, validated_data):
        files = self._pop_data(validated_data)

        db_data = models.Data.objects.create(**validated_data)
        db_data.make_dirs()

        self._create_files(db_data, files)

        db_data.save()
        return db_data

    def update(self, instance, validated_data):
        files = self._pop_data(validated_data)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        self._create_files(instance, files)
        instance.save()
        return instance

    # pylint: disable=no-self-use
    def _pop_data(self, validated_data):
        client_files = validated_data.pop('client_files')
        server_files = validated_data.pop('server_files')
        remote_files = validated_data.pop('remote_files')

        validated_data.pop('job_file_mapping', None) # optional

        for extra_key in { 'use_zip_chunks', 'use_cache', 'copy_data' }:
            validated_data.pop(extra_key)

        files = {'client_files': client_files, 'server_files': server_files, 'remote_files': remote_files}
        return files


    # pylint: disable=no-self-use
    def _create_files(self, instance, files):
        if 'client_files' in files:
            client_objects = []
            for f in files['client_files']:
                client_file = models.ClientFile(data=instance, **f)
                client_objects.append(client_file)
            models.ClientFile.objects.bulk_create(client_objects)

        if 'server_files' in files:
            for f in files['server_files']:
                server_file = models.ServerFile(data=instance, **f)
                server_file.save()

        if 'remote_files' in files:
            for f in files['remote_files']:
                remote_file = models.RemoteFile(data=instance, **f)
                remote_file.save()

class StorageSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Storage
        fields = ('id', 'location', 'cloud_storage_id')

class TaskReadSerializer(serializers.ModelSerializer):
    data_chunk_size = serializers.ReadOnlyField(source='data.chunk_size', required=False)
    data_compressed_chunk_type = serializers.ReadOnlyField(source='data.compressed_chunk_type', required=False)
    data_original_chunk_type = serializers.ReadOnlyField(source='data.original_chunk_type', required=False)
    size = serializers.ReadOnlyField(source='data.size', required=False)
    image_quality = serializers.ReadOnlyField(source='data.image_quality', required=False)
    data = serializers.ReadOnlyField(source='data.id', required=False)
    owner = BasicUserSerializer(required=False)
    assignee = BasicUserSerializer(allow_null=True, required=False)
    project_id = serializers.IntegerField(required=False, allow_null=True)
    dimension = serializers.CharField(allow_blank=True, required=False)
    target_storage = StorageSerializer(required=False, allow_null=True)
    source_storage = StorageSerializer(required=False, allow_null=True)
    jobs = JobsSummarySerializer(url_filter_key='task_id', source='segment_set')
    labels = LabelsSummarySerializer(url_filter_key='task_id')

    class Meta:
        model = models.Task
        fields = ('url', 'id', 'name', 'project_id', 'mode', 'owner', 'assignee',
            'bug_tracker', 'created_date', 'updated_date', 'overlap', 'segment_size',
            'status', 'data_chunk_size', 'data_compressed_chunk_type',
            'data_original_chunk_type', 'size', 'image_quality', 'data', 'dimension',
            'subset', 'organization', 'target_storage', 'source_storage', 'jobs', 'labels',
        )
        read_only_fields = fields
        extra_kwargs = {
            'organization': { 'allow_null': True },
            'overlap': { 'allow_null': True },
        }


class TaskWriteSerializer(WriteOnceMixin, serializers.ModelSerializer):
    labels = LabelSerializer(many=True, source='label_set', partial=True, required=False)
    owner_id = serializers.IntegerField(write_only=True, allow_null=True, required=False)
    assignee_id = serializers.IntegerField(write_only=True, allow_null=True, required=False)
    project_id = serializers.IntegerField(required=False, allow_null=True)
    target_storage = StorageSerializer(required=False, allow_null=True)
    source_storage = StorageSerializer(required=False, allow_null=True)

    class Meta:
        model = models.Task
        fields = ('url', 'id', 'name', 'project_id', 'owner_id', 'assignee_id',
            'bug_tracker', 'overlap', 'segment_size', 'labels', 'subset',
            'target_storage', 'source_storage',
        )
        write_once_fields = ('overlap', 'segment_size', 'project_id', 'owner_id', 'labels')

    def to_representation(self, instance):
        serializer = TaskReadSerializer(instance, context=self.context)
        return serializer.data

    # pylint: disable=no-self-use
    @transaction.atomic
    def create(self, validated_data):
        project_id = validated_data.get("project_id")
        if not (validated_data.get("label_set") or project_id):
            raise serializers.ValidationError('Label set or project_id must be present')
        if validated_data.get("label_set") and project_id:
            raise serializers.ValidationError('Project must have only one of Label set or project_id')

        project = None
        if project_id:
            try:
                project = models.Project.objects.get(id=project_id)
            except models.Project.DoesNotExist:
                raise serializers.ValidationError(f'The specified project #{project_id} does not exist.')

            if project.organization != validated_data.get('organization'):
                raise serializers.ValidationError(f'The task and its project should be in the same organization.')

        labels = validated_data.pop('label_set', [])

        # configure source/target storages for import/export
        storages = _configure_related_storages({
            'source_storage': validated_data.pop('source_storage', None),
            'target_storage': validated_data.pop('target_storage', None),
        })

        db_task = models.Task.objects.create(
            **storages,
            **validated_data)

        task_path = db_task.get_dirname()
        if os.path.isdir(task_path):
            shutil.rmtree(task_path)

        os.makedirs(db_task.get_task_logs_dirname())
        os.makedirs(db_task.get_task_artifacts_dirname())

        LabelSerializer.create_labels(labels, parent_instance=db_task)

        db_task.save()
        return db_task

    # pylint: disable=no-self-use
    @transaction.atomic
    def update(self, instance, validated_data):
        instance.name = validated_data.get('name', instance.name)
        instance.owner_id = validated_data.get('owner_id', instance.owner_id)
        instance.assignee_id = validated_data.get('assignee_id', instance.assignee_id)
        instance.bug_tracker = validated_data.get('bug_tracker',
            instance.bug_tracker)
        instance.subset = validated_data.get('subset', instance.subset)
        labels = validated_data.get('label_set', [])

        if instance.project_id is None:
            LabelSerializer.update_labels(labels, parent_instance=instance)

        validated_project_id = validated_data.get('project_id')
        if validated_project_id is not None and validated_project_id != instance.project_id:
            project = models.Project.objects.get(id=validated_project_id)
            if project.tasks.count() and project.tasks.first().dimension != instance.dimension:
                raise serializers.ValidationError(f'Dimension ({instance.dimension}) of the task must be the same as other tasks in project ({project.tasks.first().dimension})')
            if instance.project_id is None:
                for old_label in instance.label_set.all():
                    try:
                        if old_label.parent:
                            new_label = project.label_set.filter(name=old_label.name, parent__name=old_label.parent.name).first()
                        else:
                            new_label = project.label_set.filter(name=old_label.name).first()
                    except ValueError:
                        raise serializers.ValidationError(f'Target project does not have label with name "{old_label.name}"')
                    old_label.attributespec_set.all().delete()
                    for model in (models.LabeledTrack, models.LabeledShape, models.LabeledImage):
                        model.objects.filter(job__segment__task=instance, label=old_label).update(
                            label=new_label
                        )
                instance.label_set.all().delete()
            else:
                for old_label in instance.project.label_set.all():
                    new_label_for_name = list(filter(lambda x: x.get('id', None) == old_label.id, labels))
                    if len(new_label_for_name):
                        old_label.name = new_label_for_name[0].get('name', old_label.name)
                    try:
                        if old_label.parent:
                            new_label = project.label_set.filter(name=old_label.name, parent__name=old_label.parent.name).first()
                        else:
                            new_label = project.label_set.filter(name=old_label.name).first()
                    except ValueError:
                        raise serializers.ValidationError(f'Target project does not have label with name "{old_label.name}"')
                    for (model, attr, attr_name) in (
                        (models.LabeledTrack, models.LabeledTrackAttributeVal, 'track'),
                        (models.LabeledShape, models.LabeledShapeAttributeVal, 'shape'),
                        (models.LabeledImage, models.LabeledImageAttributeVal, 'image')
                    ):
                        attr.objects.filter(**{
                            f'{attr_name}__job__segment__task': instance,
                            f'{attr_name}__label': old_label
                        }).delete()
                        model.objects.filter(job__segment__task=instance, label=old_label).update(
                            label=new_label
                        )
            instance.project = project

        # update source and target storages
        _update_related_storages(instance, validated_data)

        instance.save()
        return instance

    def validate(self, attrs):
        # When moving task labels can be mapped to one, but when not names must be unique
        if 'project_id' in attrs.keys() and self.instance is not None:
            project_id = attrs.get('project_id')
            if project_id is not None:
                project = models.Project.objects.filter(id=project_id).first()
                if project is None:
                    raise serializers.ValidationError(f'Cannot find project with ID {project_id}')

            # Check that all labels can be mapped
            new_label_names = set()
            old_labels = self.instance.project.label_set.all() if self.instance.project_id else self.instance.label_set.all()
            new_sublabel_names = {}
            for old_label in old_labels:
                new_labels = tuple(filter(lambda x: x.get('id') == old_label.id, attrs.get('label_set', [])))
                if len(new_labels):
                    parent = new_labels[0].get('parent', old_label.parent)
                    if parent:
                        if parent.name not in new_sublabel_names:
                            new_sublabel_names[parent.name] = set()
                        new_sublabel_names[parent.name].add(new_labels[0].get('name', old_label.name))
                    else:
                        new_label_names.add(new_labels[0].get('name', old_label.name))
                else:
                    parent = old_label.parent
                    if parent:
                        if parent.name not in new_sublabel_names:
                            new_sublabel_names[parent.name] = set()
                        new_sublabel_names[parent.name].add(old_label.name)
                    else:
                        new_label_names.add(old_label.name)
            target_project = models.Project.objects.get(id=project_id)
            target_project_label_names = set()
            target_project_sublabel_names = {}
            for label in target_project.label_set.all():
                parent = label.parent
                if parent:
                    if parent.name not in target_project_sublabel_names:
                        target_project_sublabel_names[parent.name] = set()
                    target_project_sublabel_names[parent.name].add(label.name)
                else:
                    target_project_label_names.add(label.name)
            if not new_label_names.issubset(target_project_label_names):
                raise serializers.ValidationError('All task or project label names must be mapped to the target project')

            for label, sublabels in new_sublabel_names.items():
                if sublabels != target_project_sublabel_names.get(label):
                    raise serializers.ValidationError('All task or project label names must be mapped to the target project')

        return attrs

class ProjectReadSerializer(serializers.ModelSerializer):
    owner = BasicUserSerializer(required=False, read_only=True)
    assignee = BasicUserSerializer(allow_null=True, required=False, read_only=True)
    task_subsets = serializers.ListField(child=serializers.CharField(), required=False, read_only=True)
    dimension = serializers.CharField(max_length=16, required=False, read_only=True, allow_null=True)
    target_storage = StorageSerializer(required=False, allow_null=True, read_only=True)
    source_storage = StorageSerializer(required=False, allow_null=True, read_only=True)
    tasks = TasksSummarySerializer(models.Task, url_filter_key='project_id')
    labels = LabelsSummarySerializer(url_filter_key='project_id')

    class Meta:
        model = models.Project
        fields = ('url', 'id', 'name', 'owner', 'assignee',
            'bug_tracker', 'task_subsets', 'created_date', 'updated_date', 'status',
            'dimension', 'organization', 'target_storage', 'source_storage',
            'tasks', 'labels',
        )
        read_only_fields = fields
        extra_kwargs = { 'organization': { 'allow_null': True } }

    def to_representation(self, instance):
        response = super().to_representation(instance)
        task_subsets = set(instance.tasks.values_list('subset', flat=True))
        task_subsets.discard('')
        response['task_subsets'] = list(task_subsets)
        response['dimension'] = instance.tasks.first().dimension if instance.tasks.count() else None
        return response

class ProjectWriteSerializer(serializers.ModelSerializer):
    labels = LabelSerializer(write_only=True, many=True, source='label_set', partial=True, default=[])
    owner_id = serializers.IntegerField(write_only=True, allow_null=True, required=False)
    assignee_id = serializers.IntegerField(write_only=True, allow_null=True, required=False)
    task_subsets = serializers.ListField(write_only=True, child=serializers.CharField(), required=False)

    target_storage = StorageSerializer(write_only=True, required=False)
    source_storage = StorageSerializer(write_only=True, required=False)

    class Meta:
        model = models.Project
        fields = ('name', 'labels', 'owner_id', 'assignee_id', 'bug_tracker',
            'target_storage', 'source_storage', 'task_subsets',
        )

    def to_representation(self, instance):
        serializer = ProjectReadSerializer(instance, context=self.context)
        return serializer.data

    # pylint: disable=no-self-use
    @transaction.atomic
    def create(self, validated_data):
        labels = validated_data.pop('label_set')

        # configure source/target storages for import/export
        storages = _configure_related_storages({
            'source_storage': validated_data.pop('source_storage', None),
            'target_storage': validated_data.pop('target_storage', None),
        })

        db_project = models.Project.objects.create(
            **storages,
            **validated_data)

        project_path = db_project.get_dirname()
        if os.path.isdir(project_path):
            shutil.rmtree(project_path)
        os.makedirs(db_project.get_project_logs_dirname())

        LabelSerializer.create_labels(labels, parent_instance=db_project)

        return db_project

    # pylint: disable=no-self-use
    @transaction.atomic
    def update(self, instance, validated_data):
        instance.name = validated_data.get('name', instance.name)
        instance.owner_id = validated_data.get('owner_id', instance.owner_id)
        instance.assignee_id = validated_data.get('assignee_id', instance.assignee_id)
        instance.bug_tracker = validated_data.get('bug_tracker', instance.bug_tracker)
        labels = validated_data.get('label_set', [])

        LabelSerializer.update_labels(labels, parent_instance=instance)

        # update source and target storages
        _update_related_storages(instance, validated_data)

        instance.save()
        return instance

class AboutSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=128)
    description = serializers.CharField(max_length=2048)
    version = serializers.CharField(max_length=64)

class FrameMetaSerializer(serializers.Serializer):
    width = serializers.IntegerField()
    height = serializers.IntegerField()
    name = serializers.CharField(max_length=1024)
    related_files = serializers.IntegerField()

    # for compatibility with version 2.3.0
    has_related_context = serializers.SerializerMethodField()

    @extend_schema_field(serializers.BooleanField)
    def get_has_related_context(self, obj: dict) -> bool:
        return obj['related_files'] != 0

class PluginsSerializer(serializers.Serializer):
    GIT_INTEGRATION = serializers.BooleanField()
    ANALYTICS = serializers.BooleanField()
    MODELS = serializers.BooleanField()
    PREDICT = serializers.BooleanField()

class DataMetaReadSerializer(serializers.ModelSerializer):
    frames = FrameMetaSerializer(many=True, allow_null=True)
    image_quality = serializers.IntegerField(min_value=0, max_value=100)
    deleted_frames = serializers.ListField(child=serializers.IntegerField(min_value=0))

    class Meta:
        model = models.Data
        fields = (
            'chunk_size',
            'size',
            'image_quality',
            'start_frame',
            'stop_frame',
            'frame_filter',
            'frames',
            'deleted_frames',
        )
        read_only_fields = fields

class DataMetaWriteSerializer(serializers.ModelSerializer):
    deleted_frames = serializers.ListField(child=serializers.IntegerField(min_value=0))

    class Meta:
        model = models.Data
        fields = ('deleted_frames',)

class AttributeValSerializer(serializers.Serializer):
    spec_id = serializers.IntegerField()
    value = serializers.CharField(max_length=4096, allow_blank=True)

    def to_internal_value(self, data):
        data['value'] = str(data['value'])
        return super().to_internal_value(data)

class AnnotationSerializer(serializers.Serializer):
    id = serializers.IntegerField(default=None, allow_null=True)
    frame = serializers.IntegerField(min_value=0)
    label_id = serializers.IntegerField(min_value=0)
    group = serializers.IntegerField(min_value=0, allow_null=True, default=None)
    source = serializers.CharField(default='manual')

class LabeledImageSerializer(AnnotationSerializer):
    attributes = AttributeValSerializer(many=True,
        source="labeledimageattributeval_set", default=[])

class OptimizedFloatListField(serializers.ListField):
    '''Default ListField is extremely slow when try to process long lists of points'''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, child=serializers.FloatField())

    def to_internal_value(self, data):
        return self.run_child_validation(data)

    def to_representation(self, data):
        return data

    def run_child_validation(self, data):
        errors = OrderedDict()
        for idx, item in enumerate(data):
            if type(item) not in [int, float]:
                errors[idx] = exceptions.ValidationError('Value must be a float or an integer')

        if not errors:
            return data

        raise exceptions.ValidationError(errors)


class ShapeSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=models.ShapeType.choices())
    occluded = serializers.BooleanField(default=False)
    outside = serializers.BooleanField(default=False, required=False)
    z_order = serializers.IntegerField(default=0)
    rotation = serializers.FloatField(default=0, min_value=0, max_value=360)
    points = OptimizedFloatListField(
        allow_empty=True, required=False
    )

class SubLabeledShapeSerializer(ShapeSerializer, AnnotationSerializer):
    attributes = AttributeValSerializer(many=True,
        source="labeledshapeattributeval_set", default=[])

class LabeledShapeSerializer(SubLabeledShapeSerializer):
    elements = SubLabeledShapeSerializer(many=True, required=False)

class TrackedShapeSerializer(ShapeSerializer):
    id = serializers.IntegerField(default=None, allow_null=True)
    frame = serializers.IntegerField(min_value=0)
    attributes = AttributeValSerializer(many=True,
        source="trackedshapeattributeval_set", default=[])

class SubLabeledTrackSerializer(AnnotationSerializer):
    shapes = TrackedShapeSerializer(many=True, allow_empty=True,
        source="trackedshape_set")
    attributes = AttributeValSerializer(many=True,
        source="labeledtrackattributeval_set", default=[])

class LabeledTrackSerializer(SubLabeledTrackSerializer):
    elements = SubLabeledTrackSerializer(many=True, required=False)

class LabeledDataSerializer(serializers.Serializer):
    version = serializers.IntegerField(default=0) # TODO: remove
    tags   = LabeledImageSerializer(many=True, default=[])
    shapes = LabeledShapeSerializer(many=True, default=[])
    tracks = LabeledTrackSerializer(many=True, default=[])

class FileInfoSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=1024)
    type = serializers.ChoiceField(choices=["REG", "DIR"])
    mime_type = serializers.CharField(max_length=255)

class AnnotationFileSerializer(serializers.Serializer):
    annotation_file = serializers.FileField()

class DatasetFileSerializer(serializers.Serializer):
    dataset_file = serializers.FileField()

    @staticmethod
    def validate_dataset_file(value):
        if os.path.splitext(value.name)[1] != '.zip':
            raise serializers.ValidationError('Dataset file should be zip archive')
        return value

class TaskFileSerializer(serializers.Serializer):
    task_file = serializers.FileField()

class ProjectFileSerializer(serializers.Serializer):
    project_file = serializers.FileField()

class CommentReadSerializer(serializers.ModelSerializer):
    owner = BasicUserSerializer(allow_null=True, required=False)

    class Meta:
        model = models.Comment
        fields = ('id', 'issue', 'owner', 'message', 'created_date',
            'updated_date')
        read_only_fields = fields

class CommentWriteSerializer(WriteOnceMixin, serializers.ModelSerializer):
    def to_representation(self, instance):
        serializer = CommentReadSerializer(instance, context=self.context)
        return serializer.data

    class Meta:
        model = models.Comment
        fields = ('id', 'issue', 'owner', 'message', 'created_date',
            'updated_date')
        read_only_fields = ('id', 'created_date', 'updated_date', 'owner')
        write_once_fields = ('issue', )


class IssueReadSerializer(serializers.ModelSerializer):
    owner = BasicUserSerializer(allow_null=True, required=False)
    assignee = BasicUserSerializer(allow_null=True, required=False)
    position = serializers.ListField(
        child=serializers.FloatField(), allow_empty=False
    )
    comments = CommentsSummarySerializer(models.Comment, url_filter_key='issue_id')

    class Meta:
        model = models.Issue
        fields = ('id', 'frame', 'position', 'job', 'owner', 'assignee',
            'created_date', 'updated_date', 'resolved', 'comments')
        read_only_fields = fields
        extra_kwargs = {
            'created_date': { 'allow_null': True },
            'updated_date': { 'allow_null': True },
        }


class IssueWriteSerializer(WriteOnceMixin, serializers.ModelSerializer):
    position = serializers.ListField(
        child=serializers.FloatField(), allow_empty=False,
    )
    message = serializers.CharField(style={'base_template': 'textarea.html'})

    def to_representation(self, instance):
        serializer = IssueReadSerializer(instance, context=self.context)
        return serializer.data

    def create(self, validated_data):
        message = validated_data.pop('message')
        db_issue = super().create(validated_data)
        models.Comment.objects.create(issue=db_issue,
            message=message, owner=db_issue.owner)
        return db_issue

    def update(self, instance, validated_data):
        message = validated_data.pop('message', None)
        if message:
            raise NotImplementedError('Check https://github.com/cvat-ai/cvat/issues/122')
        return super().update(instance, validated_data)

    class Meta:
        model = models.Issue
        fields = ('id', 'frame', 'position', 'job', 'owner', 'assignee',
            'created_date', 'updated_date', 'message', 'resolved')
        read_only_fields = ('id', 'owner', 'created_date', 'updated_date')
        write_once_fields = ('frame', 'position', 'job', 'message', 'owner')

class ManifestSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Manifest
        fields = ('filename', )

    # pylint: disable=no-self-use
    def to_internal_value(self, data):
        return {'filename': data }

    # pylint: disable=no-self-use
    def to_representation(self, instance):
        return instance.filename if instance else instance

class CloudStorageReadSerializer(serializers.ModelSerializer):
    owner = BasicUserSerializer(required=False)
    manifests = ManifestSerializer(many=True, default=[])
    class Meta:
        model = models.CloudStorage
        exclude = ['credentials']
        read_only_fields = ('created_date', 'updated_date', 'owner', 'organization')
        extra_kwargs = { 'organization': { 'allow_null': True } }

@extend_schema_serializer(
    examples=[
        OpenApiExample(
            'Create AWS S3 cloud storage with credentials',
            description='',
            value={
                'provider_type': models.CloudProviderChoice.AWS_S3,
                'resource': 'somebucket',
                'display_name': 'Bucket',
                'credentials_type': models.CredentialsTypeChoice.KEY_SECRET_KEY_PAIR,
                'specific_attributes': 'region=eu-central-1',
                'description': 'Some description',
                'manifests': [
                    'manifest.jsonl'
                ],

            },
            request_only=True,
        ),
        OpenApiExample(
            'Create AWS S3 cloud storage without credentials',
            value={
                'provider_type': models.CloudProviderChoice.AWS_S3,
                'resource': 'somebucket',
                'display_name': 'Bucket',
                'credentials_type': models.CredentialsTypeChoice.ANONYMOUS_ACCESS,
                'manifests': [
                    'manifest.jsonl'
                ],
            },
            request_only=True,
        ),
        OpenApiExample(
            'Create Azure cloud storage',
            value={
                'provider_type': models.CloudProviderChoice.AZURE_CONTAINER,
                'resource': 'sonecontainer',
                'display_name': 'Container',
                'credentials_type': models.CredentialsTypeChoice.ACCOUNT_NAME_TOKEN_PAIR,
                'account_name': 'someaccount',
                'session_token': 'xxx',
                'manifests': [
                    'manifest.jsonl'
                ],
            },
            request_only=True,
        ),
        OpenApiExample(
            'Create GCS',
            value={
                'provider_type': models.CloudProviderChoice.GOOGLE_CLOUD_STORAGE,
                'resource': 'somebucket',
                'display_name': 'Bucket',
                'credentials_type': models.CredentialsTypeChoice.KEY_FILE_PATH,
                'key_file': 'file',
                'manifests': [
                    'manifest.jsonl'
                ],
            },
            request_only=True,
        )
    ]
)
class CloudStorageWriteSerializer(serializers.ModelSerializer):
    owner = BasicUserSerializer(required=False)
    session_token = serializers.CharField(max_length=440, allow_blank=True, required=False)
    key = serializers.CharField(max_length=40, allow_blank=True, required=False)
    secret_key = serializers.CharField(max_length=44, allow_blank=True, required=False)
    key_file = serializers.FileField(required=False)
    account_name = serializers.CharField(max_length=24, allow_blank=True, required=False)
    manifests = ManifestSerializer(many=True, default=[])

    class Meta:
        model = models.CloudStorage
        fields = (
            'provider_type', 'resource', 'display_name', 'owner', 'credentials_type',
            'created_date', 'updated_date', 'session_token', 'account_name', 'key',
            'secret_key', 'key_file', 'specific_attributes', 'description', 'id',
            'manifests', 'organization'
        )
        read_only_fields = ('created_date', 'updated_date', 'owner', 'organization')
        extra_kwargs = { 'organization': { 'allow_null': True } }

    # pylint: disable=no-self-use
    def validate_specific_attributes(self, value):
        if value:
            attributes = value.split('&')
            for attribute in attributes:
                if not len(attribute.split('=')) == 2:
                    raise serializers.ValidationError('Invalid specific attributes')
        return value

    def validate(self, attrs):
        provider_type = attrs.get('provider_type')
        if provider_type == models.CloudProviderChoice.AZURE_CONTAINER:
            if not attrs.get('account_name', ''):
                raise serializers.ValidationError('Account name for Azure container was not specified')
        return attrs

    @staticmethod
    def _manifests_validation(storage, manifests):
        # check manifest files availability
        for manifest in manifests:
            file_status = storage.get_file_status(manifest)
            if file_status == Status.NOT_FOUND:
                raise serializers.ValidationError({
                    'manifests': "The '{}' file does not exist on '{}' cloud storage" \
                        .format(manifest, storage.name)
                })
            elif file_status == Status.FORBIDDEN:
                raise serializers.ValidationError({
                    'manifests': "The '{}' file does not available on '{}' cloud storage. Access denied" \
                        .format(manifest, storage.name)
                })

    def create(self, validated_data):
        provider_type = validated_data.get('provider_type')
        should_be_created = validated_data.pop('should_be_created', None)

        key_file = validated_data.pop('key_file', None)
        # we need to save it to temporary file to check the granted permissions
        temporary_file = ''
        if key_file:
            with NamedTemporaryFile(mode='wb', prefix='cvat', delete=False) as temp_key:
                temp_key.write(key_file.read())
                temporary_file = temp_key.name
            key_file.close()
            del key_file
        credentials = Credentials(
            account_name=validated_data.pop('account_name', ''),
            key=validated_data.pop('key', ''),
            secret_key=validated_data.pop('secret_key', ''),
            session_token=validated_data.pop('session_token', ''),
            key_file_path=temporary_file,
            credentials_type = validated_data.get('credentials_type')
        )
        details = {
            'resource': validated_data.get('resource'),
            'credentials': credentials,
            'specific_attributes': parse_specific_attributes(validated_data.get('specific_attributes', ''))
        }
        storage = get_cloud_storage_instance(cloud_provider=provider_type, **details)
        if should_be_created:
            try:
                storage.create()
            except Exception as ex:
                slogger.glob.warning("Failed with creating storage\n{}".format(str(ex)))
                raise

        storage_status = storage.get_status()
        if storage_status == Status.AVAILABLE:
            manifests = [m.get('filename') for m in validated_data.pop('manifests')]
            self._manifests_validation(storage, manifests)

            db_storage = models.CloudStorage.objects.create(
                credentials=credentials.convert_to_db(),
                **validated_data
            )
            db_storage.save()

            manifest_file_instances = [models.Manifest(filename=manifest, cloud_storage=db_storage) for manifest in manifests]
            models.Manifest.objects.bulk_create(manifest_file_instances)

            cloud_storage_path = db_storage.get_storage_dirname()
            if os.path.isdir(cloud_storage_path):
                shutil.rmtree(cloud_storage_path)

            os.makedirs(db_storage.get_storage_logs_dirname(), exist_ok=True)
            if temporary_file:
                # so, gcs key file is valid and we need to set correct path to the file
                real_path_to_key_file = db_storage.get_key_file_path()
                shutil.copyfile(temporary_file, real_path_to_key_file)
                os.remove(temporary_file)

                credentials.key_file_path = real_path_to_key_file
                db_storage.credentials = credentials.convert_to_db()
                db_storage.save()
            return db_storage
        elif storage_status == Status.FORBIDDEN:
            field = 'credentials'
            message = 'Cannot create resource {} with specified credentials. Access forbidden.'.format(storage.name)
        else:
            field = 'resource'
            message = 'The resource {} not found. It may have been deleted.'.format(storage.name)
        if temporary_file:
            os.remove(temporary_file)
        slogger.glob.error(message)
        raise serializers.ValidationError({field: message})

    # pylint: disable=no-self-use
    def update(self, instance, validated_data):
        credentials = Credentials()
        credentials.convert_from_db({
            'type': instance.credentials_type,
            'value': instance.credentials,
        })
        credentials_dict = {k:v for k,v in validated_data.items() if k in {
            'key','secret_key', 'account_name', 'session_token', 'key_file_path',
            'credentials_type'
        }}

        key_file = validated_data.pop('key_file', None)
        temporary_file = ''
        if key_file:
            with NamedTemporaryFile(mode='wb', prefix='cvat', delete=False) as temp_key:
                temp_key.write(key_file.read())
                temporary_file = temp_key.name
            credentials_dict['key_file_path'] = temporary_file
            key_file.close()
            del key_file

        credentials.mapping_with_new_values(credentials_dict)
        instance.credentials = credentials.convert_to_db()
        instance.credentials_type = validated_data.get('credentials_type', instance.credentials_type)
        instance.resource = validated_data.get('resource', instance.resource)
        instance.display_name = validated_data.get('display_name', instance.display_name)
        instance.description = validated_data.get('description', instance.description)
        instance.specific_attributes = validated_data.get('specific_attributes', instance.specific_attributes)

        # check cloud storage existing
        details = {
            'resource': instance.resource,
            'credentials': credentials,
            'specific_attributes': parse_specific_attributes(instance.specific_attributes)
        }
        storage = get_cloud_storage_instance(cloud_provider=instance.provider_type, **details)
        storage_status = storage.get_status()
        if storage_status == Status.AVAILABLE:
            new_manifest_names = set(i.get('filename') for i in validated_data.get('manifests', []))
            previos_manifest_names = set(i.filename for i in instance.manifests.all())
            delta_to_delete = tuple(previos_manifest_names - new_manifest_names)
            delta_to_create = tuple(new_manifest_names - previos_manifest_names)
            if delta_to_delete:
                instance.manifests.filter(filename__in=delta_to_delete).delete()
            if delta_to_create:
                # check manifest files existing
                self._manifests_validation(storage, delta_to_create)
                manifest_instances = [models.Manifest(filename=f, cloud_storage=instance) for f in delta_to_create]
                models.Manifest.objects.bulk_create(manifest_instances)
            if temporary_file:
                # so, gcs key file is valid and we need to set correct path to the file
                real_path_to_key_file = instance.get_key_file_path()
                shutil.copyfile(temporary_file, real_path_to_key_file)
                os.remove(temporary_file)

                instance.credentials = real_path_to_key_file
            instance.save()
            return instance
        elif storage_status == Status.FORBIDDEN:
            field = 'credentials'
            message = 'Cannot update resource {} with specified credentials. Access forbidden.'.format(storage.name)
        else:
            field = 'resource'
            message = 'The resource {} not found. It may have been deleted.'.format(storage.name)
        if temporary_file:
            os.remove(temporary_file)
        slogger.glob.error(message)
        raise serializers.ValidationError({field: message})

class RelatedFileSerializer(serializers.ModelSerializer):

    class Meta:
        model = models.RelatedFile
        fields = '__all__'
        read_only_fields = ('path',)


def _update_related_storages(instance, validated_data):
    for storage in ('source_storage', 'target_storage'):
        new_conf = validated_data.pop(storage, None)

        if not new_conf:
            continue

        cloud_storage_id = new_conf.get('cloud_storage_id')
        if cloud_storage_id:
            _validate_existence_of_cloud_storage(cloud_storage_id)

        # storage_instance maybe None
        storage_instance = getattr(instance, storage)
        if not storage_instance:
            storage_instance = models.Storage(**new_conf)
            storage_instance.save()
            setattr(instance, storage, storage_instance)
            continue

        new_location = new_conf.get('location')
        storage_instance.location = new_location or storage_instance.location
        storage_instance.cloud_storage_id = new_conf.get('cloud_storage_id', \
            storage_instance.cloud_storage_id if not new_location else None)

        cloud_storage_id = storage_instance.cloud_storage_id
        if cloud_storage_id:
            try:
                _ = models.CloudStorage.objects.get(id=cloud_storage_id)
            except models.CloudStorage.DoesNotExist:
                raise serializers.ValidationError(f'The specified cloud storage {cloud_storage_id} does not exist.')

        storage_instance.save()

def _configure_related_storages(validated_data):

    storages = {
        'source_storage': None,
        'target_storage': None,
    }

    for i in storages:
        storage_conf = validated_data.get(i)
        if storage_conf:
            cloud_storage_id = storage_conf.get('cloud_storage_id')
            if cloud_storage_id:
                _validate_existence_of_cloud_storage(cloud_storage_id)
            storage_instance = models.Storage(**storage_conf)
            storage_instance.save()
            storages[i] = storage_instance
    return storages

def _validate_existence_of_cloud_storage(cloud_storage_id):
    try:
        _ = models.CloudStorage.objects.get(id=cloud_storage_id)
    except models.CloudStorage.DoesNotExist:
        raise serializers.ValidationError(f'The specified cloud storage {cloud_storage_id} does not exist.')
