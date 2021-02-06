# Generated by Django 3.1.4 on 2021-02-01 14:12

import os
from collections import defaultdict

from django.conf import settings
from django.db import migrations
from django.utils.text import slugify
from translate.misc.xml_helpers import valid_chars_only

from weblate.formats.ttkit import TBXFormat
from weblate.utils.hash import calculate_hash
from weblate.utils.state import STATE_READONLY, STATE_TRANSLATED
from weblate.vcs.git import LocalRepository


def create_glossary(project, name, slug, glossary, license):
    return project.component_set.create(
        slug=slug,
        name=name,
        is_glossary=True,
        glossary_name=glossary.name,
        glossary_color=glossary.color,
        allow_translation_propagation=False,
        manage_units=True,
        file_format="tbx",
        filemask="*.tbx",
        vcs="local",
        repo="local:",
        branch="main",
        source_language=glossary.source_language,
        license=license,
    )


def migrate_glossaries(apps, schema_editor):  # noqa: C901
    Project = apps.get_model("trans", "Project")
    Language = apps.get_model("lang", "Language")
    db_alias = schema_editor.connection.alias

    projects = Project.objects.using(db_alias).all()

    total = len(projects)
    processed = 0

    for processed, project in enumerate(projects):
        component_slugs = set(project.component_set.values_list("slug", flat=True))
        percent = int(100 * processed / total)
        print(f"Migrating glossaries {percent}% [{processed}/{total}]...{project.name}")
        glossaries = project.glossary_set.all()

        try:
            license = project.component_set.exclude(license="").values_list(
                "license", flat=True
            )[0]
        except IndexError:
            license = ""

        for glossary in glossaries:
            if len(glossaries) == 1:
                name = "Glossary"
                slug = "glossary"
            else:
                name = f"Glossary: {glossary.name}"
                slug = "glossary-{}".format(slugify(glossary.name))

            base_name = name
            base_slug = slug

            # Create component
            attempts = 0
            while True:
                if slug not in component_slugs:
                    component = create_glossary(project, name, slug, glossary, license)
                    component_slugs.add(slug)
                    break
                attempts += 1
                name = f"{base_name} - {attempts}"
                slug = f"{base_slug}-{attempts}"

            repo_path = os.path.join(settings.DATA_DIR, "vcs", project.slug, slug)

            # Create VCS repository
            repo = LocalRepository.from_files(repo_path, {})

            # Migrate links
            component.links.set(glossary.links.all())

            # Create source translation
            source_translation = component.translation_set.create(
                language=glossary.source_language,
                check_flags="read-only",
                filename="",
                plural=glossary.source_language.plural_set.filter(source=0)[0],
                language_code=glossary.source_language.code,
            )
            source_units = {}

            # Get list of languages
            languages = Language.objects.filter(term__glossary=glossary).distinct()

            # Migrate ters
            for language in languages:
                base_filename = f"{language.code}.tbx"
                filename = os.path.join(repo_path, base_filename)
                is_source = language == source_translation.language
                # Create translation object
                if is_source:
                    translation = source_translation
                else:
                    translation = component.translation_set.create(
                        language=language,
                        plural=language.plural_set.filter(source=0)[0],
                        filename=base_filename,
                        language_code=language.code,
                    )

                # Create store file
                TBXFormat.create_new_file(filename, language.code, "")
                store = TBXFormat(filename, language_code=language.code)
                sources = defaultdict(int)
                for position, term in enumerate(
                    glossary.term_set.filter(language=language)
                ):
                    source = valid_chars_only(term.source)
                    target = valid_chars_only(term.target)
                    # Store to the file
                    sources[source] += 1
                    if sources[source] > 1:
                        context = str(sources[source])
                    else:
                        context = ""
                    id_hash = calculate_hash(source, context)
                    if id_hash not in source_units:
                        source_units[id_hash] = source_translation.unit_set.create(
                            context=context,
                            source=source,
                            target=source,
                            state=STATE_READONLY,
                            position=position,
                            num_words=len(source.split()),
                            id_hash=id_hash,
                        )
                        source_units[id_hash].source_unit = source_units[id_hash]
                        source_units[id_hash].save()
                    store.new_unit(context, source, target)
                    # Migrate database
                    if is_source:
                        unit = source_units[id_hash]
                        unit.target = target
                        unit.save(update_fields=["target"])
                    else:
                        unit = translation.unit_set.create(
                            context=context,
                            source=source,
                            target=target,
                            state=STATE_TRANSLATED,
                            position=position,
                            num_words=len(source.split()),
                            id_hash=id_hash,
                            source_unit=source_units[id_hash],
                        )
                    # Adjust history entries (langauge and project should be already set)
                    term.change_set.update(
                        unit=unit,
                        translation=translation,
                        component=component,
                    )
                store.save()

                # Update translation hash
                translation.revision = repo.get_object_hash(filename)
                translation.save(update_fields=["revision"])

            # Commit files
            with repo.lock:
                repo.execute(["add", repo_path])
                if repo.needs_commit():
                    repo.commit("Migrate glossary content")
    print(f"Migrating glossaries completed [{total}/{total}]")


class Migration(migrations.Migration):

    dependencies = [
        ("trans", "0115_auto_20210201_1305"),
        ("glossary", "0005_set_source_language"),
    ]

    operations = [migrations.RunPython(migrate_glossaries, elidable=True)]
