# Generated manually for DatabaseEngine / LogicalDatabase refactor

import django.db.models.deletion
from django.db import migrations, models


def forwards_logical(apps, schema_editor):
    ManagedDatabase = apps.get_model("dbinstances", "ManagedDatabase")
    LogicalDatabase = apps.get_model("dbinstances", "LogicalDatabase")
    for m in ManagedDatabase.objects.all():
        dbname = (m.mysql_database or "").strip()
        if dbname:
            LogicalDatabase.objects.create(
                managed_database=m,
                schema_name=dbname,
                label="",
            )


def forwards_m2m(apps, schema_editor):
    ManagedDatabaseUser = apps.get_model("dbinstances", "ManagedDatabaseUser")
    LogicalDatabase = apps.get_model("dbinstances", "LogicalDatabase")
    for u in ManagedDatabaseUser.objects.all():
        raw = getattr(u, "default_database", None)
        if not raw:
            continue
        name = str(raw).strip()
        if not name:
            continue
        ld = LogicalDatabase.objects.filter(
            managed_database_id=u.managed_database_id,
            schema_name=name,
        ).first()
        if ld is not None:
            u.granted_databases.add(ld)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("dbinstances", "0002_managed_database_user"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="manageddatabaseuser",
            name="dbinstances_manageddb_single_root",
        ),
        migrations.CreateModel(
            name="LogicalDatabase",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "schema_name",
                    models.CharField(
                        help_text="MySQL database name (CREATE DATABASE).",
                        max_length=64,
                    ),
                ),
                (
                    "label",
                    models.CharField(
                        blank=True,
                        help_text="Optional display label in admin.",
                        max_length=200,
                    ),
                ),
                (
                    "managed_database",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="logical_databases",
                        to="dbinstances.manageddatabase",
                    ),
                ),
            ],
            options={
                "ordering": ["schema_name"],
            },
        ),
        migrations.AddConstraint(
            model_name="logicaldatabase",
            constraint=models.UniqueConstraint(
                fields=("managed_database", "schema_name"),
                name="dbinstances_logicaldb_unique_per_engine",
            ),
        ),
        migrations.RunPython(forwards_logical, noop_reverse),
        migrations.AddField(
            model_name="manageddatabaseuser",
            name="granted_databases",
            field=models.ManyToManyField(
                blank=True,
                help_text="For application users: schemas this account may access (ALL PRIVILEGES on each schema.*). Empty means *.* (dev only).",
                related_name="users_granted",
                to="dbinstances.logicaldatabase",
            ),
        ),
        migrations.RunPython(forwards_m2m, noop_reverse),
        migrations.RemoveField(
            model_name="manageddatabaseuser",
            name="default_database",
        ),
        migrations.RemoveField(
            model_name="manageddatabase",
            name="mysql_database",
        ),
        migrations.RenameModel(
            old_name="ManagedDatabase",
            new_name="DatabaseEngine",
        ),
        migrations.RenameField(
            model_name="databaseengine",
            old_name="engine",
            new_name="vendor",
        ),
        migrations.RenameField(
            model_name="manageddatabaseuser",
            old_name="managed_database",
            new_name="engine",
        ),
        migrations.RemoveConstraint(
            model_name="logicaldatabase",
            name="dbinstances_logicaldb_unique_per_engine",
        ),
        migrations.RenameField(
            model_name="logicaldatabase",
            old_name="managed_database",
            new_name="engine",
        ),
        migrations.AddConstraint(
            model_name="logicaldatabase",
            constraint=models.UniqueConstraint(
                fields=("engine", "schema_name"),
                name="dbinstances_logicaldb_unique_per_engine",
            ),
        ),
        migrations.AddConstraint(
            model_name="manageddatabaseuser",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "root")),
                fields=("engine",),
                name="dbinstances_engine_single_root",
            ),
        ),
    ]
