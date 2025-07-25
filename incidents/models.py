# -*- coding: utf-8 -*-
import datetime
import logging

from colorfield.fields import ColorField
from django.db.models.signals import post_save, post_delete
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from django.dispatch import Signal, receiver
from django.db import models
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _
from django.conf import settings

from treebeard.mp_tree import MP_Node

from fir_artifacts import artifacts
from fir_artifacts.models import Artifact, File
from fir_plugins.models import link_to
from incidents.authorization import tree_authorization, AuthorizationModelMixin


CONFIDENTIALITY_LEVEL = (
    (0, "C0"),
    (1, "C1"),
    (2, "C2"),
    (3, "C3"),
)

LIGHT_MODE_CHOICES = (("light", "light"), ("dark", "dark"))

# Special Model class that handles signals


model_created = Signal()
model_updated = Signal()
model_status_changed = Signal()


class FIRModel:
    def done_creating(self):
        model_created.send(sender=self.__class__, instance=self)

    def done_updating(self):
        model_updated.send(sender=self.__class__, instance=self)


# Profile ====================================================================


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    incident_number = models.IntegerField(default=50)
    hide_closed = models.BooleanField(default=False)
    light_mode = models.CharField(
        max_length=10, choices=LIGHT_MODE_CHOICES, default="light"
    )

    def __str__(self):
        return "Profile for user '{}'".format(self.user)


# Audit trail ================================================================


class Log(models.Model):
    who = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, null=True, blank=True, db_constraint=False
    )
    what = models.CharField(max_length=100)
    when = models.DateTimeField(auto_now_add=True)
    incident = models.ForeignKey(
        "Incident",
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        db_constraint=False,
    )
    comment = models.ForeignKey(
        "Comments",
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        db_constraint=False,
    )
    inst_type = None

    def __str__(self):
        if self.inst_type == Incident:
            incident_id = self.incident.id
            if getattr(settings, "INCIDENT_SHOW_ID", False):
                incident_id = getattr(settings, "INCIDENT_ID_PREFIX", "") + str(
                    self.incident.id
                )
            return "[%s] %s: %s (%s)" % (self.when, self.what, incident_id, self.who)
        elif self.inst_type == Comments:
            incident_id = self.comment.incident.id
            if getattr(settings, "INCIDENT_SHOW_ID", False):
                incident_id = getattr(settings, "INCIDENT_ID_PREFIX", "") + str(
                    self.comment.incident.id
                )
            inc_str = "event"
            if self.comment.incident.is_incident:
                inc_str = "incident"

            return "[%s] %s on %s %s (%s)" % (
                self.when,
                self.what,
                inc_str,
                incident_id,
                self.who,
            )
        else:
            return "[%s] %s (%s)" % (self.when, self.what, self.who)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        logging.getLogger("FIR").info(str(self).split("] ", 1)[1])

    @staticmethod
    def log(what, user, incident=None, comment=None, inst_type=None):
        log = Log()
        log.what = what
        log.who = user
        log.incident = incident
        log.comment = comment
        log.inst_type = inst_type
        log.save()


class LabelGroup(models.Model):
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name


class Label(models.Model):
    name = models.CharField(max_length=50)
    group = models.ForeignKey(LabelGroup, on_delete=models.CASCADE)

    def __str__(self):
        return "%s" % (self.name)


class BusinessLine(MP_Node, AuthorizationModelMixin):
    name = models.CharField(max_length=100)

    def __str__(self):
        parents = list(self.get_ancestors())
        parents.append(self)
        return " > ".join([bl.name for bl in parents])

    class Meta:
        verbose_name = _("business line")

    def get_incident_count(self, query):
        incident_count = self.incident_set.filter(query).distinct().count()
        incident_count += (
            Incident.objects.filter(query)
            .filter(concerned_business_lines__in=self.get_descendants())
            .distinct()
            .count()
        )
        return incident_count


class AccessControlEntry(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name=_("user")
    )
    business_line = models.ForeignKey(
        BusinessLine,
        on_delete=models.CASCADE,
        verbose_name=_("business line"),
        related_name="acl",
    )
    role = models.ForeignKey(
        "auth.Group", on_delete=models.CASCADE, verbose_name=_("role")
    )

    def __str__(self):
        return _("{} is {} on {}").format(self.user, self.role, self.business_line)

    class Meta:
        verbose_name = _("access control entry")
        verbose_name_plural = _("access control entries")


class BaleCategory(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, null=True)
    category_number = models.IntegerField()
    parent_category = models.ForeignKey(
        "BaleCategory", on_delete=models.CASCADE, null=True, blank=True
    )

    class Meta:
        verbose_name_plural = "Bale categories"

    def __str__(self):
        if self.parent_category:
            return "(%s > %s) %s" % (
                self.parent_category.category_number,
                self.category_number,
                self.name,
            )
        else:
            return "(%s) %s" % (self.category_number, self.name)


class IncidentCategory(models.Model):
    name = models.CharField(max_length=100)
    bale_subcategory = models.ForeignKey(BaleCategory, on_delete=models.CASCADE)
    is_major = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "Incident categories"

    def __str__(self):
        return self.name


# Core models ================================================================


def datetimenow():
    return datetime.datetime.now().replace(second=0, microsecond=0)


class IncidentStatus(models.Model):
    name = models.CharField(
        max_length=50,
        validators=[RegexValidator('"', inverse_match=True)],
    )
    icon = models.CharField(
        max_length=50,
        validators=[RegexValidator("^[-_A-Za-z0-9]*$")],
    )
    associated_action = models.ForeignKey(
        Label,
        on_delete=models.SET_NULL,
        limit_choices_to={"group__name": "action"},
        related_name="status_action_label",
        blank=True,
        null=True,
    )
    flag = models.CharField(
        max_length=50,
        choices=(
            ("initial", "Initial status"),
            ("final", "Final status"),
        ),
        null=True,
        blank=True,
    )

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.flag == "initial":
            if (
                IncidentStatus.objects.exclude(pk=self.pk)
                .filter(flag="initial")
                .exists()
            ):
                raise ValidationError(_("There can only be one initial status."))
        super().save(*args, **kwargs)

    class Meta:
        verbose_name_plural = _("Incident statuses")


def get_initial_status():
    return IncidentStatus.objects.get(flag="initial")


@tree_authorization(
    fields=[
        "concerned_business_lines",
    ],
    tree_model="incidents.BusinessLine",
    owner_field="opened_by",
    owner_permission=settings.INCIDENT_CREATOR_PERMISSION,
)
@link_to(File)
@link_to(Artifact)
class Incident(FIRModel, models.Model):
    date = models.DateTimeField(default=datetimenow, blank=True)
    is_starred = models.BooleanField(default=False)
    subject = models.CharField(max_length=256)
    description = models.TextField()
    category = models.ForeignKey(IncidentCategory, on_delete=models.CASCADE)
    concerned_business_lines = models.ManyToManyField(BusinessLine, blank=True)
    main_business_lines = models.ManyToManyField(
        BusinessLine, related_name="incidents_affecting_main", blank=True
    )
    detection = models.ForeignKey(
        Label,
        on_delete=models.CASCADE,
        limit_choices_to={"group__name": "detection"},
        related_name="detection_label",
    )
    severity = models.ForeignKey(
        "SeverityChoice", null=True, blank=True, on_delete=models.SET_NULL
    )
    is_incident = models.BooleanField(default=False)
    is_major = models.BooleanField(default=False)
    actor = models.ForeignKey(
        Label,
        on_delete=models.CASCADE,
        limit_choices_to={"group__name": "actor"},
        related_name="actor_label",
        blank=True,
        null=True,
    )
    plan = models.ForeignKey(
        Label,
        on_delete=models.CASCADE,
        limit_choices_to={"group__name": "plan"},
        related_name="plan_label",
        blank=True,
        null=True,
    )
    status = models.ForeignKey(
        "IncidentStatus",
        on_delete=models.CASCADE,
        default=get_initial_status,
        null=False,
        blank=False,
    )
    opened_by = models.ForeignKey(User, on_delete=models.CASCADE)
    confidentiality = models.IntegerField(choices=CONFIDENTIALITY_LEVEL, default="1")

    def __str__(self):
        return self.subject

    def close_timeout(self, username="cert"):
        previous_status = self.status
        self.status = IncidentStatus.objects.filter(flag="final").first()
        self.save()
        model_status_changed.send(
            sender=Incident, instance=self, previous_status=previous_status
        )

        c = Comments()
        c.comment = "Incident closed (timeout)"
        c.date = datetime.datetime.now()
        c.action = IncidentStatus.objects.filter(flag="final").first().associated_action
        c.incident = self
        c.opened_by = User.objects.get(username=username)
        c.save()

    def get_business_lines_names(self):
        return ", ".join([b.name for b in self.concerned_business_lines.all()])

    def refresh_main_business_lines(self):
        mainbls = set()
        for bl in self.concerned_business_lines.all():
            mainbls.add(bl.get_root())
        self.main_business_lines.set(list(mainbls))

    def refresh_artifacts(self, data=""):
        if data == "":
            coms = self.comments_set.all()

            data = self.description
            for c in coms:
                data += "\n" + c.comment

        found_artifacts = artifacts.find(data)

        artifact_list = []
        for key in found_artifacts:
            for a in found_artifacts[key]:
                artifact_list.append((key, a))

        db_artifacts = Artifact.objects.filter(value__in=[a[1] for a in artifact_list])

        exist = []

        for a in db_artifacts:
            exist.append((a.type, a.value))
            if self not in a.incidents.all():
                a.incidents.add(self)

        new_artifacts = list(set(artifact_list) - set(exist))
        all_artifacts = list(set(artifact_list))

        for a in new_artifacts:
            new = Artifact(type=a[0], value=a[1])
            new.save()
            new.incidents.add(self)

        for a in all_artifacts:
            artifacts.after_save(a[0], a[1], self)

    class Meta:
        permissions = (
            ("handle_incidents", "Can handle incidents"),
            ("report_events", "Can report events"),
            ("view_incidents", "Can view incidents"),
            ("view_statistics", "Can view statistics"),
        )


class Comments(models.Model):
    date = models.DateTimeField(default=datetime.datetime.now, blank=True)
    comment = models.TextField()
    action = models.ForeignKey(
        Label,
        on_delete=models.CASCADE,
        limit_choices_to={"group__name": "action"},
        related_name="action_label",
    )
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE)
    opened_by = models.ForeignKey(User, on_delete=models.CASCADE)

    class Meta:
        verbose_name_plural = "comments"

    def __str__(self):
        return "Comment for incident %s" % self.incident.id

    @classmethod
    def create_diff_comment(cls, incident, data, user):
        if isinstance(data, Incident):
            new = getattr(data, "status", None)
        else:
            new = data.get("status", None)
        old = getattr(incident, "status", None)

        if new is not None and new != old:
            Comments.objects.create(
                comment="Status changed to '%s'" % new,
                action=Label.objects.get(name="Info"),
                incident=incident,
                opened_by=user,
            )


class Attribute(models.Model):
    name = models.CharField(max_length=50)
    value = models.CharField(max_length=200)
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE)

    def __str__(self):
        return "%s: %s" % (self.name, self.value)


class ValidAttribute(models.Model):
    name = models.CharField(max_length=50)
    unit = models.CharField(max_length=50, null=True, blank=True)
    description = models.CharField(max_length=500, null=True, blank=True)
    categories = models.ManyToManyField(IncidentCategory)

    def __str__(self):
        return self.name


class SeverityChoice(models.Model):
    name = models.CharField(
        max_length=50,
        validators=[RegexValidator("[a-zA-Z0-9]+")],
    )
    color = ColorField(default="#777")

    def __str__(self):
        return self.name


# Templating =================================================================


class IncidentTemplate(models.Model):
    name = models.CharField(max_length=100)
    subject = models.CharField(max_length=256, null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    category = models.ForeignKey(
        IncidentCategory, on_delete=models.CASCADE, null=True, blank=True
    )
    concerned_business_lines = models.ManyToManyField(BusinessLine, blank=True)
    detection = models.ForeignKey(
        Label,
        on_delete=models.CASCADE,
        limit_choices_to={"group__name": "detection"},
        null=True,
        blank=True,
    )
    severity = models.ForeignKey(
        "SeverityChoice", null=True, blank=True, on_delete=models.SET_NULL
    )
    is_incident = models.BooleanField(default=False)
    actor = models.ForeignKey(
        Label,
        on_delete=models.CASCADE,
        limit_choices_to={"group__name": "actor"},
        related_name="+",
        blank=True,
        null=True,
    )
    plan = models.ForeignKey(
        Label,
        on_delete=models.CASCADE,
        limit_choices_to={"group__name": "plan"},
        related_name="+",
        blank=True,
        null=True,
    )

    def __str__(self):
        return self.name


#
# Signal receivers
#


@receiver(model_created, sender=Incident)
@receiver(model_updated, sender=Incident)
def refresh_incident(sender, instance, **kwargs):
    instance.refresh_artifacts(instance.description)


# Automatically create comments


@receiver(post_save, sender=Incident)
def comment_new_incident(sender, instance, created, **kwargs):
    if created:
        status = IncidentStatus.objects.get(flag="initial")

        Comments.objects.create(
            comment="Incident opened",
            action=status.associated_action,
            incident=instance,
            opened_by=instance.opened_by,
            date=instance.date,
        )


# Automatically log actions


@receiver(post_save, sender=Incident)
def log_new_incident(sender, instance, created, **kwargs):
    obj = "Event"
    action = "edited"
    if instance.is_incident:
        obj = "Incident"
    if created:
        action = "created"
    Log.log(
        f"{obj} {action}",
        instance.opened_by,
        incident=instance,
        inst_type=type(instance),
    )


@receiver(post_delete, sender=Incident)
def log_delete_incident(sender, instance, *args, **kwargs):
    obj = "event"
    if instance.is_incident:
        obj = "incident"
    Log.log(
        f"{obj} deleted",
        instance.opened_by,
        incident=instance,
        inst_type=type(instance),
    )


@receiver(post_save, sender=Comments)
def log_new_comment(sender, instance, created, **kwargs):
    action = "edited"
    if created:
        action = "created"
    Log.log(
        f"Comment {instance.id} {action}",
        instance.opened_by,
        comment=instance,
        inst_type=type(instance),
    )


@receiver(post_delete, sender=Comments)
def log_delete_comment(sender, instance, *args, **kwargs):
    Log.log(
        f"Comment {instance.id} deleted",
        instance.opened_by,
        comment=instance,
        inst_type=type(instance),
    )
