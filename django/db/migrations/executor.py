from __future__ import unicode_literals

from django.db import migrations
from django.apps.registry import apps as global_apps
from .loader import MigrationLoader
from .recorder import MigrationRecorder
from .state import ProjectState

class MigrationExecutor(object):
    """
    End-to-end migration execution - loads migrations, and runs them
    up or down to a specified set of targets.
    """

    def __init__(self, connection, progress_callback=None, render_tolerance=8):
        self.connection = connection
        self.loader = MigrationLoader(self.connection)
        self.recorder = MigrationRecorder(self.connection)
        self.progress_callback = progress_callback
        self.render_tolerance = render_tolerance

    def migration_plan(self, targets, clean_start=False):
        """
        Given a set of targets, returns a list of (Migration instance, backwards?).
        """
        plan = []
        if clean_start:
            applied = set()
        else:
            applied = set(self.loader.applied_migrations)
        for target in targets:
            # If the target is (app_label, None), that means unmigrate everything
            if target[1] is None:
                for root in self.loader.graph.root_nodes():
                    if root[0] == target[0]:
                        for migration in self.loader.graph.backwards_plan(root):
                            if migration in applied:
                                plan.append((self.loader.graph.nodes[migration], True))
                                applied.remove(migration)
            # If the migration is already applied, do backwards mode,
            # otherwise do forwards mode.
            elif target in applied:
                # Don't migrate backwards all the way to the target node (that
                # may roll back dependencies in other apps that don't need to
                # be rolled back); instead roll back through target's immediate
                # child(ren) in the same app, and no further.
                next_in_app = sorted(
                    n for n in
                    self.loader.graph.dependents.get(target, set())
                    if n[0] == target[0]
                )
                backwards = False
                for node in next_in_app:
                    for migration in self.loader.graph.backwards_plan(node):
                        if migration in applied:
                            backwards = True
                            plan.append((self.loader.graph.nodes[migration], True))
                            applied.remove(migration)
                if not backwards:
                    for migration in self.loader.graph.forwards_plan(target):
                        if migration not in applied:
                            plan.append((self.loader.graph.nodes[migration], False))
                            applied.add(migration)
            else:
                for migration in self.loader.graph.forwards_plan(target):
                    if migration not in applied:
                        plan.append((self.loader.graph.nodes[migration], False))
                        applied.add(migration)
        return plan

    def migrate(self, targets, plan=None, fake=False):
        """
        Migrates the database up to the given targets.
        """
        if plan is None:
            plan = self.migration_plan(targets)
        # Create the forwards plan Django would follow on an empty database
        full_plan = self.migration_plan(self.loader.graph.leaf_nodes(), clean_start=True)
        # Holds all states right before and right after a migration is applied
        # if the migration is being run.

        all_forwards = all(not backwards for mig, backwards in plan)
        all_backwards = all(backwards for mig, backwards in plan)

        if not plan:
            pass  # Nothing to do for an empty plan
        elif all_forwards == all_backwards:
            # This should only happen if there's a mixed plan
            raise Exception(
                "Migration plans with both forwards and backwards migrations "
                "are not supported. Please split your migration process into "
                "separate plans of only forwards OR backwards migrations."
            )
        elif all_forwards:
            self._migrate_all_forwards(plan, full_plan, fake=fake)
        else:
            # No need to check for `elif all_backwards` here, as that condition
            # would always evaluate to true.
            self._migrate_all_backwards(plan, full_plan, fake=fake)

    def _migrate_all_forwards(self, plan, full_plan, fake):
        """
        Take a list of 2-tuples of the form (migration instance, False) and
        apply them in the order they occur in the full_plan.
        """
        state = ProjectState(real_apps=list(self.loader.unmigrated_apps))
        second_state = None
        migrations_to_run = {m[0] for m in plan}
        # estimate how many migrations we have to run and determine render points
        last_to_apply = None
        applied_count = 0
        for migration, _ in full_plan:
            if migration in migrations_to_run:
                last_to_apply = migration
                applied_count = 0
            else:
                applied_count += 1
                if applied_count > self.render_tolerance and last_to_apply:
                    last_to_apply._trigger_rerender = True

        for migration, _ in full_plan:
            if not migrations_to_run:
                # We remove every migration that we applied from this set so
                # that we can bail out once the last migration has been applied
                # and don't always run until the very end of the migration
                # process.
                break
            if migration in migrations_to_run:
                if 'apps' not in state.__dict__:
                    if self.progress_callback:
                        self.progress_callback("render_start", migration, False)
                    state.apps  # Render all -- performance critical
                    if self.progress_callback:
                        self.progress_callback("render_end", migration, False)
                if second_state and 'apps' not in second_state.__dict__:
                    if self.progress_callback:
                        self.progress_callback("render_start", migration, True)
                    second_state.apps  # Render all -- performance critical
                    if self.progress_callback:
                        self.progress_callback("render_end", migration, True)
                state, second_state = self.apply_migration(state, migration, fake=fake, second_state=second_state,
                                                           rerender=hasattr(migration, '_trigger_rerender'))
                migrations_to_run.remove(migration)
            else:
                if self.progress_callback:
                    self.progress_callback("mutating_start", migration, False)
                migration.mutate_state(state, preserve=False)
                if self.progress_callback:
                    self.progress_callback("mutating_end", migration, False)
                if second_state:
                    if self.progress_callback:
                        self.progress_callback("mutating_start", migration, True)
                    migration.mutate_state(second_state, preserve=False)
                    if self.progress_callback:
                        self.progress_callback("mutating_end", migration, True)

    def _migrate_all_backwards(self, plan, full_plan, fake):
        """
        Take a list of 2-tuples of the form (migration instance, True) and
        unapply them in reverse order they occur in the full_plan.
        Since unapplying a migration requires the project state prior to that
        migration, Django will compute the migration states before each of them
        in a first run over the plan and then unapply them in a second run over
        the plan.
        """
        migrations_to_run = {m[0] for m in plan}
        # Holds all migration states prior to the migrations being unapplied
        states = {}
        state = ProjectState(real_apps=list(self.loader.unmigrated_apps))
        for migration, _ in full_plan:
            if not migrations_to_run:
                # We remove every migration that we applied from this set so
                # that we can bail out once the last migration has been applied
                # and don't always run until the very end of the migration
                # process.
                break
            if migration in migrations_to_run:
                if 'apps' not in state.__dict__:
                    state.apps  # Render all -- performance critical
                # The state before this migration
                states[migration] = state
                # The old state keeps as-is, we continue with the new state
                state = migration.mutate_state(state, preserve=True)
                migrations_to_run.remove(migration)
            else:
                migration.mutate_state(state, preserve=False)
                self.progress_callback("mutating_end", migration, fake)
        for migration, _ in plan:
            self.unapply_migration(states[migration], migration, fake=fake)

    def collect_sql(self, plan):
        """
        Takes a migration plan and returns a list of collected SQL
        statements that represent the best-efforts version of that plan.
        """
        statements = []
        state = None
        for migration, backwards in plan:
            with self.connection.schema_editor(collect_sql=True) as schema_editor:
                if state is None:
                    state = self.loader.project_state((migration.app_label, migration.name), at_end=False)
                if not backwards:
                    state, _ = migration.apply(state, schema_editor, collect_sql=True)
                else:
                    state = migration.unapply(state, schema_editor, collect_sql=True)
            statements.extend(schema_editor.collected_sql)
        return statements

    def apply_migration(self, state, migration, fake=False, second_state=None, rerender=False):
        """
        Runs a migration forwards.
        """
        if self.progress_callback:
            self.progress_callback("apply_start", migration, fake)
        if not fake:
            # Test to see if this is an already-applied initial migration
            applied, state = self.detect_soft_applied(state, migration)
            if applied:
                fake = True
                second_state = None
            else:
                # Alright, do it normally
                with self.connection.schema_editor() as schema_editor:
                    state, second_state = migration.apply(state, schema_editor, second_state=second_state,
                                                          rerender=rerender)
        # For replacement migrations, record individual statuses
        if migration.replaces:
            for app_label, name in migration.replaces:
                self.recorder.record_applied(app_label, name)
        else:
            self.recorder.record_applied(migration.app_label, migration.name)
        # Report progress
        if self.progress_callback:
            self.progress_callback("apply_success", migration, fake)
        return state, second_state

    def unapply_migration(self, state, migration, fake=False):
        """
        Runs a migration backwards.
        """
        if self.progress_callback:
            self.progress_callback("unapply_start", migration, fake)
        if not fake:
            with self.connection.schema_editor() as schema_editor:
                state = migration.unapply(state, schema_editor)
        # For replacement migrations, record individual statuses
        if migration.replaces:
            for app_label, name in migration.replaces:
                self.recorder.record_unapplied(app_label, name)
        else:
            self.recorder.record_unapplied(migration.app_label, migration.name)
        # Report progress
        if self.progress_callback:
            self.progress_callback("unapply_success", migration, fake)
        return state

    def detect_soft_applied(self, project_state, migration):
        """
        Tests whether a migration has been implicitly applied - that the
        tables it would create exist. This is intended only for use
        on initial migrations (as it only looks for CreateModel).
        """
        # Bail if the migration isn't the first one in its app
        if [name for app, name in migration.dependencies if app == migration.app_label]:
            return False, project_state
        if project_state is None:
            after_state = self.loader.project_state((migration.app_label, migration.name), at_end=True)
        else:
            after_state = migration.mutate_state(project_state)
        apps = after_state.apps
        found_create_migration = False
        # Make sure all create model are done
        for operation in migration.operations:
            if isinstance(operation, migrations.CreateModel):
                model = apps.get_model(migration.app_label, operation.name)
                if model._meta.swapped:
                    # We have to fetch the model to test with from the
                    # main app cache, as it's not a direct dependency.
                    model = global_apps.get_model(model._meta.swapped)
                if model._meta.db_table not in self.connection.introspection.table_names(self.connection.cursor()):
                    return False, project_state
                found_create_migration = True
        # If we get this far and we found at least one CreateModel migration,
        # the migration is considered implicitly applied.
        return found_create_migration, after_state
