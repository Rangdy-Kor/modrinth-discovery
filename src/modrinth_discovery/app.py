from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QListWidget, QListWidgetItem, QVBoxLayout, QWidget)

from .api import ModrinthAPI
from .config import CONFIG
from .diagnostics import configure_diagnostics, get_logger
from .database import Database
from .feed import filter_unseen
from .installer import ExistingModConflict, ModInstaller
from .inventory import build_inventory, validate_inventory
from .batch import BatchInstaller
from .trash import TrashManager
from .cache import PersistentCache
from .reviewed_service import ReviewedService
from .library import filter_entries
from .instances import InstanceDiscoveryError, discover_instances, manual_instance
from .models import Decision, DecisionSnapshot, InstallPlan, Instance, LibraryEntry, Project, SearchIndex
from .workers import Worker, WorkerController

logger = get_logger("gui")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__(); self.setWindowTitle("Modrinth Discovery"); self.resize(820, 760)
        self.api=ModrinthAPI(timeout=CONFIG.timeout_seconds); self.database=Database(CONFIG.database_path)
        self.installer=ModInstaller(self.api); self.pool=QThreadPool.globalInstance()
        self.batch=BatchInstaller(self.api,self.installer); self.trash=TrashManager(CONFIG.database_path.parent/"trash")
        self.cache=PersistentCache(CONFIG.database_path); self.reviewed_service=ReviewedService(self.api,self.cache)
        self.workers=WorkerController(self.pool)
        self.instances: list[Instance]=[]; self.projects: list[Project]=[]; self.current_index=0
        self.offset=0; self.total_hits=None; self.seen_ids:set[str]=set(); self.busy=False
        self.history:list[list[tuple[Project,DecisionSnapshot|None]]]=[]; self.library_decision:Decision|None=None; self.collection_view=False; self._busy_token=0
        self._library_all:list[LibraryEntry]=[]
        self._review_generation=0; self._review_items={}; self._review_state={}
        self._build_ui(); self._setup_shortcuts(); self._load_instances()

    def _build_ui(self) -> None:
        root=QWidget(); layout=QVBoxLayout(root); form=QFormLayout()
        instance_row=QHBoxLayout(); self.instance_combo=QComboBox(); self.browse_button=QPushButton("폴더 선택…")
        self.instance_combo.currentIndexChanged.connect(self._instance_changed); self.browse_button.clicked.connect(self._browse_instance)
        instance_row.addWidget(self.instance_combo, 1); instance_row.addWidget(self.browse_button); form.addRow("인스턴스", instance_row)
        filters=QHBoxLayout(); self.version_edit=QLineEdit(); self.loader_combo=QComboBox()
        self.loader_combo.addItems(["fabric","quilt","forge","neoforge"]); self.index_combo=QComboBox()
        for index in SearchIndex: self.index_combo.addItem(index.value.title(), index.value)
        self.index_combo.setCurrentIndex(self.index_combo.findData(SearchIndex.DOWNLOADS.value))
        filters.addWidget(QLabel("Minecraft")); filters.addWidget(self.version_edit)
        filters.addWidget(QLabel("Loader")); filters.addWidget(self.loader_combo)
        filters.addWidget(QLabel("정렬")); filters.addWidget(self.index_combo)
        self.apply_button=QPushButton("탐색 시작"); self.apply_button.clicked.connect(self.reset_feed); filters.addWidget(self.apply_button)
        form.addRow("필터", filters); layout.addLayout(form)
        mode_row=QHBoxLayout(); self.normal_button=QPushButton("Discovery"); self.maybe_review_button=QPushButton("Maybe")
        self.skipped_review_button=QPushButton("Skipped")
        self.reviewed_button=QPushButton("Reviewed"); self.installed_view_button=QPushButton("Installed")
        self.normal_button.clicked.connect(self.reset_feed); self.maybe_review_button.clicked.connect(lambda:self.show_library(Decision.MAYBE))
        self.skipped_review_button.clicked.connect(lambda:self.show_library(Decision.SKIPPED))
        self.reviewed_button.clicked.connect(self.show_reviewed); self.installed_view_button.clicked.connect(self.show_installed)
        mode_row.addWidget(self.normal_button); mode_row.addWidget(self.maybe_review_button); mode_row.addWidget(self.skipped_review_button); mode_row.addWidget(self.reviewed_button); mode_row.addWidget(self.installed_view_button); mode_row.addStretch(); layout.addLayout(mode_row)
        self.icon_label=QLabel(); self.icon_label.setFixedSize(128,128); self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label=QLabel(); font=self.title_label.font(); font.setPointSize(20); font.setBold(True); self.title_label.setFont(font)
        self.author_label=QLabel(); self.description_label=QLabel(); self.description_label.setWordWrap(True)
        self.metadata_label=QLabel(); self.metadata_label.setWordWrap(True); self.status_label=QLabel()
        self.collection_list=QListWidget(); self.collection_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection); self.collection_list.hide()
        self.collection_list.itemDoubleClicked.connect(lambda _item:self.open_selected_library())
        self.collection_list.itemSelectionChanged.connect(self._update_collection_actions)
        self.collection_actions=QWidget(); collection_buttons=QHBoxLayout(self.collection_actions)
        self.library_filter=QLineEdit(); self.library_filter.setPlaceholderText("제목 또는 slug 검색…")
        self.library_sort=QComboBox(); self.library_sort.addItem("Title","title"); self.library_sort.addItem("First reviewed","first_reviewed_at"); self.library_sort.addItem("Last reviewed","last_reviewed_at")
        self.library_filter.textChanged.connect(self._render_library); self.library_sort.currentIndexChanged.connect(self._reload_library)
        self.open_selected_button=QPushButton("Modrinth 열기"); self.to_maybe_button=QPushButton("→ Maybe"); self.to_skipped_button=QPushButton("→ Skipped"); self.to_reviewed_button=QPushButton("→ Reviewed")
        self.open_selected_button.clicked.connect(self.open_selected_library)
        self.to_maybe_button.clicked.connect(lambda:self.change_selected_decision(Decision.MAYBE)); self.to_skipped_button.clicked.connect(lambda:self.change_selected_decision(Decision.SKIPPED)); self.to_reviewed_button.clicked.connect(lambda:self.change_selected_decision(Decision.REVIEWED))
        self.individual_install_button=QPushButton("선택 항목 설치"); self.batch_button=QPushButton("설치 가능한 항목 일괄 설치")
        self.library_remove_button=QPushButton("제거")
        self.refresh_button=QPushButton("Refresh")
        self.remove_button=QPushButton("선택한 설치 파일 제거"); self.validate_button=QPushButton("인스턴스 검사")
        self.auto_dependencies=QCheckBox("Automatically install required dependencies")
        self.auto_dependencies.setChecked(self.database.get_bool_setting("auto_dependencies",False)); self.auto_dependencies.toggled.connect(lambda v:self.database.set_bool_setting("auto_dependencies",v))
        self.individual_install_button.clicked.connect(self.install_selected_reviewed); self.batch_button.clicked.connect(self.plan_batch)
        self.library_remove_button.clicked.connect(self.remove_selected_library)
        self.refresh_button.clicked.connect(lambda:self.show_reviewed(force=True))
        self.remove_button.clicked.connect(self.remove_installed); self.validate_button.clicked.connect(self.validate_installed)
        for w in (self.library_filter,self.library_sort,self.open_selected_button,self.to_maybe_button,self.to_skipped_button,self.to_reviewed_button,self.library_remove_button,self.individual_install_button,self.batch_button,self.refresh_button,self.remove_button,self.validate_button,self.auto_dependencies): collection_buttons.addWidget(w)
        self.collection_actions.hide()
        layout.addWidget(self.icon_label, alignment=Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.title_label)
        layout.addWidget(self.author_label); layout.addWidget(self.description_label); layout.addWidget(self.metadata_label); layout.addStretch()
        layout.addWidget(self.collection_list); layout.addWidget(self.collection_actions)
        self.progress=QProgressBar(); self.progress.setRange(0,100); self.progress.hide(); layout.addWidget(self.progress); layout.addWidget(self.status_label)
        buttons=QHBoxLayout(); self.skip_button=QPushButton("Skip [1]"); self.maybe_button=QPushButton("Maybe [2]")
        self.install_button=QPushButton("Reviewed / Check [3]"); self.web_button=QPushButton("Modrinth [O]"); self.undo_button=QPushButton("Undo [Ctrl+Z]")
        self.skip_button.clicked.connect(lambda: self.record_and_next(Decision.SKIPPED)); self.maybe_button.clicked.connect(lambda: self.record_and_next(Decision.MAYBE))
        self.install_button.clicked.connect(lambda:self.record_and_next(Decision.REVIEWED)); self.web_button.clicked.connect(self.open_web); self.undo_button.clicked.connect(self.undo)
        for button in (self.skip_button,self.maybe_button,self.install_button,self.web_button,self.undo_button): buttons.addWidget(button)
        layout.addLayout(buttons); self.setCentralWidget(root)

    def _setup_shortcuts(self) -> None:
        for key, callback in (("1", lambda: self.record_and_next(Decision.SKIPPED)),
                              ("2", lambda: self.record_and_next(Decision.MAYBE)),
                              ("3", lambda:self.record_and_next(Decision.REVIEWED)), ("O", self.open_web), ("Ctrl+Z", self.undo)):
            shortcut=QShortcut(QKeySequence(key), self); shortcut.setContext(Qt.ShortcutContext.WindowShortcut); shortcut.activated.connect(callback)

    def _load_instances(self) -> None:
        try: self.instances=discover_instances()
        except InstanceDiscoveryError as error: QMessageBox.warning(self,"인스턴스 탐색",str(error))
        self.instance_combo.clear()
        for instance in self.instances: self.instance_combo.addItem(instance.display_name)
        if not self.instances:
            self.title_label.setText("Modrinth App 인스턴스를 찾지 못했습니다")
            self.description_label.setText("'폴더 선택…'으로 인스턴스 디렉터리를 직접 선택하세요.")
            self._set_controls(False)
        else: self._instance_changed(0)

    def _browse_instance(self) -> None:
        selected=QFileDialog.getExistingDirectory(self,"Modrinth 인스턴스 디렉터리 선택")
        if not selected: return
        try: instance=manual_instance(Path(selected), minecraft_version=self.version_edit.text().strip(), loader=self.loader_combo.currentText())
        except InstanceDiscoveryError as error: QMessageBox.warning(self,"잘못된 경로",str(error)); return
        self.instances.append(instance); self.instance_combo.addItem(instance.display_name); self.instance_combo.setCurrentIndex(len(self.instances)-1)

    def _instance_changed(self, index: int) -> None:
        if index < 0 or index >= len(self.instances): return
        instance=self.instances[index]; self.version_edit.setText(instance.minecraft_version)
        loader_index=self.loader_combo.findText(instance.loader); self.loader_combo.setCurrentIndex(max(loader_index,0)); self.reset_feed()

    def selected_instance(self) -> Instance | None:
        index=self.instance_combo.currentIndex(); return self.instances[index] if 0 <= index < len(self.instances) else None

    def reset_feed(self) -> None:
        if not self.selected_instance(): return
        if not self.version_edit.text().strip():
            QMessageBox.warning(self,"필터 필요","Minecraft 버전을 입력해 주세요."); return
        self._review_generation+=1; self.library_decision=None; self.collection_view=False; self._show_discovery_widgets(True); self.skip_button.setText("Skip [1]"); self.projects=[]; self.current_index=0; self.offset=0; self.total_hits=None; self.seen_ids=set(); self._request_page()

    def _request_page(self) -> None:
        if self.busy or (self.total_hits is not None and self.offset >= self.total_hits):
            self.show_current_project(); return
        token=self._set_busy(True,"프로젝트를 불러오는 중…")
        worker=Worker(self.api.search_projects, minecraft_version=self.version_edit.text().strip(),
            loader=self.loader_combo.currentText(), index=self.index_combo.currentData(), limit=CONFIG.page_size,
            offset=self.offset, name="search-projects")
        worker.signals.result.connect(self._page_loaded); worker.signals.error.connect(self._task_error)
        self.workers.start(worker, lambda: self._finish_busy(token))

    def _page_loaded(self, page) -> None:
        self._set_busy(False)
        self.offset=page.offset+page.limit; self.total_hits=page.total_hits
        self.projects.extend(filter_unseen(page.projects, self.database.reviewed_ids(), self.seen_ids))
        if self.current_project() is None and self.offset < page.total_hits:
            self._request_page(); return
        self.show_current_project()

    def current_project(self) -> Project | None:
        return self.projects[self.current_index] if self.current_index < len(self.projects) else None

    def show_current_project(self) -> None:
        project=self.current_project()
        if project is None:
            if self.library_decision is None and not self.busy and (self.total_hits is None or self.offset < self.total_hits): self._request_page(); return
            self.title_label.setText("검토할 프로젝트가 없습니다"); self.author_label.clear(); self.description_label.setText("현재 조건의 피드를 모두 검토했습니다.")
            self.metadata_label.clear(); self.icon_label.clear(); self.update_status(); return
        self.title_label.setText(project.title); self.author_label.setText(f"by {project.author}" if project.author else "")
        self.description_label.setText(project.description)
        environment=", ".join(project.environment) if project.environment else "정보 없음"
        self.metadata_label.setText(f"{self.version_edit.text()} · {self.loader_combo.currentText().title()} · mod\n{project.downloads:,} downloads · Updated: {project.date_modified or '—'}\nEnvironment: {environment}")
        self.icon_label.clear(); self._load_icon(project); self.update_status()
        if self.library_decision is None and len(self.projects)-self.current_index <= CONFIG.prefetch_threshold and not self.busy: self._request_page()

    def _load_icon(self, project: Project) -> None:
        if not project.icon_url: return
        worker=Worker(self.api.get_bytes, project.icon_url, name="download-icon")
        worker.signals.result.connect(lambda data, pid=project.project_id: self._icon_loaded(pid,data))
        worker.signals.error.connect(lambda error: logger.info("icon.error error=%s", error))
        self.workers.start(worker)

    def _icon_loaded(self, project_id: str, data: bytes) -> None:
        if not self.current_project() or self.current_project().project_id != project_id: return
        pixmap=QPixmap(); pixmap.loadFromData(data); self.icon_label.setPixmap(pixmap.scaled(128,128,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))

    def record_and_next(self, decision: Decision) -> None:
        if self.busy or self.collection_view: return
        project=self.current_project()
        if not project: return
        previous=self.database.decision_snapshot(project.project_id)
        self.database.save_decision(project,decision); self.history.append([(project,previous)]); self.current_index+=1; self.show_current_project()
        if decision==Decision.REVIEWED: self.status_label.setText(f"✓ {project.title}을 Reviewed에 추가했습니다.")

    def undo(self) -> None:
        if self.busy or not self.history: return
        if self.collection_view and self.library_decision is None:return
        changes=self.history.pop(); self.database.restore_snapshots(changes)
        if self.library_decision is not None:
            self._review_generation+=1; self._reload_library()
            if self.library_decision == Decision.REVIEWED:
                self.refresh_button.setEnabled(True); self.batch_button.setEnabled(True)
            return
        project,_previous=changes[0]
        if self.current_index and self.projects[self.current_index-1].project_id==project.project_id: self.current_index-=1
        else: self.projects.insert(self.current_index,project)
        self.show_current_project()

    def install(self) -> None:
        if self.busy: return
        project=self.current_project(); instance=self.selected_instance()
        if not project or not instance: return
        logger.info("install.clicked project=%s instance=%s game=%s loader=%s",
                    project.project_id, instance.path, self.version_edit.text().strip(), self.loader_combo.currentText())
        token=self._set_busy(True,"호환 버전과 dependency를 확인하는 중…")
        worker=Worker(self._prepare_install,project,instance,name="prepare-install")
        worker.signals.result.connect(lambda plan: self._confirm_install(project,instance,plan))
        worker.signals.error.connect(self._task_error)
        self.workers.start(worker, lambda: self._finish_busy(token))

    def _prepare_install(self, project: Project, instance: Instance) -> InstallPlan:
        logger.info("install.prepare.version.start project=%s", project.project_id)
        mod_file=self.api.get_latest_file(project.project_id,minecraft_version=self.version_edit.text().strip(),loader=self.loader_combo.currentText())
        logger.info("install.prepare.version.done project=%s version=%s dependencies=%d",
                    project.project_id, mod_file.version_id, len(mod_file.dependencies))
        return self.installer.prepare(mod_file,instance.mods_directory)

    def _confirm_install(self, project: Project, instance: Instance, plan: InstallPlan) -> None:
        logger.info("install.prepare.success project=%s", project.project_id)
        self._set_busy(False)
        deps="\n".join(f"• {d.dependency.dependency_type}: {d.dependency.project_id or d.dependency.file_name or d.dependency.version_id or 'external'} (설치 여부 확인 불가)" for d in plan.dependencies) or "없음"
        text=f"{project.title}\n버전: {plan.mod_file.version_number}\n파일: {plan.mod_file.filename}\n\nDependencies:\n{deps}\n\n설치하시겠습니까?"
        if QMessageBox.question(self,"설치 확인",text) == QMessageBox.StandardButton.Yes: self._run_install(project,instance,plan,())

    def _run_install(self, project: Project, instance: Instance, plan: InstallPlan, replace_paths: tuple[Path,...]) -> None:
        logger.info("install.download.dispatch project=%s replacements=%d", project.project_id, len(replace_paths))
        token=self._set_busy(True,"다운로드 중…",True)
        worker=Worker(self.installer.install,plan,instance.mods_directory,replace_paths=replace_paths,
                      with_progress=True,name="download-install")
        worker.signals.progress.connect(self.progress.setValue); worker.signals.result.connect(lambda result: self._installed(project,result))
        worker.signals.error.connect(lambda error: self._install_error(project,instance,plan,error))
        self.workers.start(worker, lambda: self._finish_busy(token))

    def _install_error(self, project: Project, instance: Instance, plan: InstallPlan, error: Exception) -> None:
        logger.error("install.download.error project=%s error=%r", project.project_id, error)
        self._set_busy(False)
        if isinstance(error,ExistingModConflict):
            details="\n".join(f"• {m.name or m.path.name}: {m.version or '버전 미상'}" for m in error.conflicts)
            if QMessageBox.question(self,"기존 모드 교체",f"동일한 mod ID가 이미 설치되어 있습니다.\n\n{details}\n\n기존 파일을 새 버전으로 교체할까요?")==QMessageBox.StandardButton.Yes:
                self._run_install(project,instance,plan,tuple(m.path for m in error.conflicts)); return
        self._task_error(error)

    def _installed(self, project: Project, result) -> None:
        logger.info("install.download.success project=%s", project.project_id)
        self._set_busy(False)
        path, plan=result
        QMessageBox.information(self,"설치 완료",f"{project.title}\n버전: {plan.mod_file.version_number}\n파일: {path.name}")
        self.current_index+=1; self.show_current_project()

    def _show_discovery_widgets(self, visible: bool) -> None:
        for widget in (self.icon_label,self.title_label,self.author_label,self.description_label,
                       self.metadata_label,self.skip_button,self.maybe_button,self.install_button,
                       self.web_button): widget.setVisible(visible)
        self.undo_button.setVisible(True)
        self.collection_list.setVisible(not visible); self.collection_actions.setVisible(not visible)

    def _configure_library_actions(self, decision: Decision | None) -> None:
        """Show only the controls that belong to the active collection screen."""
        is_library=decision is not None
        for widget in (self.library_filter,self.library_sort,self.open_selected_button):
            widget.setVisible(is_library)
        self.to_maybe_button.setVisible(is_library and decision != Decision.MAYBE)
        self.to_skipped_button.setVisible(is_library and decision != Decision.SKIPPED)
        self.to_reviewed_button.setVisible(is_library and decision != Decision.REVIEWED)
        self.library_remove_button.setVisible(is_library)
        reviewed=decision == Decision.REVIEWED
        for widget in (self.individual_install_button,self.batch_button,self.refresh_button,self.auto_dependencies):
            widget.setVisible(reviewed)
        self.remove_button.setVisible(decision is None)
        self.validate_button.setVisible(decision is None)
        self._update_collection_actions()

    def _update_collection_actions(self) -> None:
        has_selection=bool(self.collection_list.selectedItems())
        for widget in (self.open_selected_button,self.to_maybe_button,self.to_skipped_button,
                       self.to_reviewed_button,self.library_remove_button,self.remove_button):
            widget.setEnabled(has_selection)
        self.individual_install_button.setEnabled(len(self.collection_list.selectedItems()) == 1)

    def show_library(self, decision: Decision) -> None:
        if decision == Decision.REVIEWED:
            self.show_reviewed(); return
        if self.busy:return
        self._review_generation+=1; self.library_decision=decision; self.collection_view=True
        self._show_discovery_widgets(False); self._configure_library_actions(decision)
        self._review_state={}; self._reload_library()
        self.status_label.setText(f"{decision.value.title()}: {len(self._library_all)}")

    def _reload_library(self, *_args) -> None:
        if self.library_decision is None:return
        sort_by=self.library_sort.currentData() or "last_reviewed_at"
        self._library_all=self.database.library_entries(self.library_decision,sort_by)
        self._render_library()

    def _render_library(self, *_args) -> None:
        if self.library_decision is None:return
        entries=filter_entries(self._library_all,self.library_filter.text())
        self.collection_list.clear(); self._review_items={}
        for entry in entries:
            project=entry.project
            if self.library_decision == Decision.REVIEWED:
                state=self._review_state.get(project.project_id,{})
                mod=state.get("installed"); status=state.get("status"); mod_file=state.get("file")
                local=f"Installed {mod.version or ''}" if mod else "Not installed"
                remote="확인 중…" if status is None else (f"Latest {mod_file.version_number}" if mod_file else ("No compatible version" if status.value=="incompatible" else "Unknown"))
                text=f"{project.title} — {local} — {remote}"
            else:
                text=(f"{project.title}  ({project.slug})\n"
                      f"First: {entry.first_reviewed_at} · Last: {entry.last_reviewed_at}")
            item=QListWidgetItem(text); item.setData(Qt.ItemDataRole.UserRole,project)
            self.collection_list.addItem(item)
            if self.library_decision == Decision.REVIEWED:self._review_items[project.project_id]=item
        self._update_collection_actions()

    def open_selected_library(self) -> None:
        selected=self.collection_list.selectedItems()
        if not selected:return
        project=selected[0].data(Qt.ItemDataRole.UserRole)
        if isinstance(project,Project):webbrowser.open(project.project_url)

    def change_selected_decision(self, decision: Decision) -> None:
        if self.busy or self.library_decision is None or decision == self.library_decision:return
        projects=[item.data(Qt.ItemDataRole.UserRole) for item in self.collection_list.selectedItems()]
        project_ids=[project.project_id for project in projects if isinstance(project,Project)]
        if not project_ids:return
        changes=self.database.change_decisions(project_ids,decision)
        if changes:self.history.append(changes)
        count=len(changes); self._review_generation+=1; self._reload_library()
        if self.library_decision == Decision.REVIEWED:
            self.refresh_button.setEnabled(True); self.batch_button.setEnabled(True)
        self.status_label.setText(f"{count}개 항목을 {decision.value.title()}로 이동했습니다.")

    def remove_selected_library(self) -> None:
        """Forget selected judgments without touching any instance files or inventory."""
        if self.busy or self.library_decision is None:return
        projects=[item.data(Qt.ItemDataRole.UserRole) for item in self.collection_list.selectedItems()]
        project_ids=[project.project_id for project in projects if isinstance(project,Project)]
        if not project_ids:return
        changes=self.database.delete_decisions(project_ids)
        if not changes:return
        self.history.append(changes); self._review_generation+=1
        self._reload_library()
        if self.library_decision == Decision.REVIEWED:
            self.refresh_button.setEnabled(True); self.batch_button.setEnabled(True)
        self.status_label.setText(f"{len(changes)}개 항목을 개인 라이브러리에서 제거했습니다. Undo로 복원할 수 있습니다.")

    def show_reviewed(self, force: bool=False) -> None:
        instance=self.selected_instance()
        if self.busy or not instance: return
        started=time.perf_counter(); self.library_decision=Decision.REVIEWED; self.collection_view=True
        self._library_all=self.database.library_entries(Decision.REVIEWED,self.library_sort.currentData() or "last_reviewed_at")
        projects=[entry.project for entry in self._library_all]; sqlite_elapsed=time.perf_counter()-started
        self._show_discovery_widgets(False); self._configure_library_actions(Decision.REVIEWED)
        self._review_generation+=1; generation=self._review_generation; self._review_state={project.project_id:{"project":project} for project in projects}
        ui_started=time.perf_counter(); self._render_library(); ui_elapsed=time.perf_counter()-ui_started
        self.status_label.setText(f"Reviewed: {len(projects)} · 로컬 목록 표시 완료 · 원격 정보 확인 중…")
        self.refresh_button.setEnabled(False); self.batch_button.setEnabled(False)
        logger.info("reviewed.immediate sqlite=%.4fs ui_model=%.4fs count=%d",sqlite_elapsed,ui_elapsed,len(projects))
        worker=Worker(self.reviewed_service.load,projects,instance,force=force,with_items=True,name="reviewed-progressive")
        worker.signals.item.connect(lambda update,g=generation:self._reviewed_item(g,update))
        worker.signals.result.connect(lambda timing,g=generation:self._reviewed_finished(g,timing,sqlite_elapsed,ui_elapsed))
        worker.signals.error.connect(self._task_error); self.workers.start(worker,lambda:self._reviewed_worker_finished(generation))

    def _reviewed_item(self, generation: int, update: dict) -> None:
        if generation!=self._review_generation:return
        pid=update["project_id"]; state=self._review_state.get(pid)
        if not state:return
        state.update(update); project=state["project"]; mod=state.get("installed"); status=state.get("status"); file=state.get("file")
        local=f"Installed {mod.version or ''}" if mod else "Not installed"
        remote="확인 중…" if status is None else (f"Latest {file.version_number}" if file else ("No compatible version" if status.value=="incompatible" else "Unknown"))
        item=self._review_items.get(pid)
        if item is not None:item.setText(f"{project.title} — {local} — {remote}")

    def _reviewed_finished(self, generation: int, timing: dict, sqlite_elapsed: float, ui_elapsed: float) -> None:
        if generation!=self._review_generation:return
        installed=sum(bool(s.get("installed")) for s in self._review_state.values())
        self.status_label.setText(f"Reviewed: {len(self._review_state)} · Installed: {installed} · 확인 완료 {timing['total']:.2f}s")
        logger.info("reviewed.complete sqlite=%.4f ui_model=%.4f details=%s",sqlite_elapsed,ui_elapsed,timing)

    def _reviewed_worker_finished(self, generation: int) -> None:
        if generation==self._review_generation:
            self.refresh_button.setEnabled(True); self.batch_button.setEnabled(True)

    def show_installed(self) -> None:
        instance=self.selected_instance()
        if self.busy or not instance:return
        self._review_generation+=1; self.library_decision=None; self.collection_view=True; self._show_discovery_widgets(False); self.collection_list.clear()
        self._configure_library_actions(None)
        token=self._set_busy(True,"설치된 모드의 hash를 확인하는 중…")
        worker=Worker(lambda:self.cache.inventory(self.api,instance)[0],name="installed-inventory")
        worker.signals.result.connect(self._installed_loaded); worker.signals.error.connect(self._task_error)
        self.workers.start(worker,lambda:self._finish_busy(token))

    def _installed_loaded(self, inventory) -> None:
        self._set_busy(False)
        for mod in inventory:
            title=mod.project_title or mod.name or mod.path.name
            identity=f"Modrinth {mod.project_id} / {mod.version_id}" if mod.project_id else "Unverified"
            item=QListWidgetItem(f"{title} — {mod.version or 'version unknown'} — {identity}")
            item.setData(Qt.ItemDataRole.UserRole,mod); self.collection_list.addItem(item)
        self.status_label.setText(f"Installed files: {len(inventory)}")

    def install_selected_reviewed(self) -> None:
        selected=self.collection_list.selectedItems()
        if len(selected)!=1:
            QMessageBox.information(self,"항목 선택","설치할 Reviewed 프로젝트 하나를 선택하세요."); return
        project=selected[0].data(Qt.ItemDataRole.UserRole); instance=self.selected_instance()
        if project and instance: self._set_busy(False); self._prepare_reviewed_install(project,instance)

    def _prepare_reviewed_install(self, project: Project, instance: Instance) -> None:
        token=self._set_busy(True,"설치 계획을 확인하는 중…")
        automatic=self.auto_dependencies.isChecked()
        def work():
            root=self._prepare_install(project,instance); inventory=build_inventory(self.api,instance.mods_directory)
            dependency_plan=self.batch.resolver.resolve([root.mod_file],minecraft_version=instance.minecraft_version,
                loader=instance.loader,installed_project_ids={m.project_id for m in inventory if m.project_id},
                automatic=automatic)
            return root,dependency_plan
        worker=Worker(work,name="reviewed-install-plan")
        worker.signals.result.connect(lambda result:self._confirm_reviewed_bundle(project,instance,*result)); worker.signals.error.connect(self._task_error)
        self.workers.start(worker,lambda:self._finish_busy(token))

    def _confirm_reviewed_bundle(self, project: Project, instance: Instance, root: InstallPlan, dependency_plan) -> None:
        from .models import BatchPlan, BatchPlanItem, BatchStatus
        self._set_busy(False)
        if not self.auto_dependencies.isChecked(): self._confirm_install(project,instance,root); return
        items=[]
        for file in dependency_plan.files:
            target=project if file.project_id==project.project_id else Project(file.project_id,file.project_id,
                f"Dependency {file.project_id}","","",0,None,f"https://modrinth.com/mod/{file.project_id}","")
            items.append(BatchPlanItem(target,BatchStatus.INSTALLABLE,file))
        plan=BatchPlan(tuple(items),dependency_plan.issues)
        summary=f"설치 파일: {len(items)}\n확인 필요: {len(dependency_plan.issues)}"
        if QMessageBox.question(self,"설치 계획",summary+"\n\n적용할까요?")==QMessageBox.StandardButton.Yes:
            token=self._set_busy(True,"dependency 포함 설치 중…",True); worker=Worker(self.batch.apply,plan,instance.mods_directory,name="dependency-apply")
            worker.signals.result.connect(self._batch_done); worker.signals.error.connect(self._task_error); self.workers.start(worker,lambda:self._finish_busy(token))

    def plan_batch(self) -> None:
        instance=self.selected_instance()
        if self.busy or not instance:return
        projects=self.database.selected_projects(); automatic=self.auto_dependencies.isChecked(); token=self._set_busy(True,"Reviewed 일괄 설치 dry-run 중…")
        def work():
            plan=self.reviewed_service.batch_plan(projects,instance)
            if not automatic:return plan
            from .models import BatchPlan,BatchPlanItem,BatchStatus
            inventory,_=self.cache.inventory(self.api,instance); installed={m.project_id for m in inventory if m.project_id}
            roots=[i.mod_file for i in plan.items if i.status==BatchStatus.INSTALLABLE and i.mod_file]
            deps=self.batch.resolver.resolve(roots,minecraft_version=instance.minecraft_version,loader=instance.loader,
                                             installed_project_ids=installed,automatic=True)
            items=list(plan.items); existing={i.project.project_id for i in items}
            for file in deps.files:
                if file.project_id not in existing:
                    project=Project(file.project_id,file.project_id,f"Dependency {file.project_id}","","",0,None,f"https://modrinth.com/mod/{file.project_id}","")
                    items.append(BatchPlanItem(project,BatchStatus.INSTALLABLE,file,"required dependency"))
            return BatchPlan(tuple(items),deps.issues)
        worker=Worker(work,name="batch-plan"); worker.signals.result.connect(lambda plan:self._batch_planned(instance,plan)); worker.signals.error.connect(self._task_error)
        self.workers.start(worker,lambda:self._finish_busy(token))

    def _batch_planned(self, instance: Instance, plan) -> None:
        from .models import BatchStatus
        self._set_busy(False); summary="\n".join(f"{s.value}: {plan.count(s)}" for s in BatchStatus)
        if plan.dependency_issues:
            summary+=f"\n확인 필요한 dependency: {len(plan.dependency_issues)}"
        if plan.count(BatchStatus.INSTALLABLE) and QMessageBox.question(self,"Batch dry-run",summary+"\n\n설치 가능한 항목을 적용할까요?")==QMessageBox.StandardButton.Yes:
            token=self._set_busy(True,"일괄 설치 중…",True); worker=Worker(self.batch.apply,plan,instance.mods_directory,name="batch-apply")
            worker.signals.result.connect(self._batch_done); worker.signals.error.connect(self._task_error); self.workers.start(worker,lambda:self._finish_busy(token))

    def _batch_done(self, results) -> None:
        self._set_busy(False); ok=sum(r.success for r in results)
        QMessageBox.information(self,"Batch 결과",f"성공: {ok}\n실패: {len(results)-ok}")
        self.show_reviewed()

    def remove_installed(self) -> None:
        instance=self.selected_instance(); mods=[i.data(Qt.ItemDataRole.UserRole) for i in self.collection_list.selectedItems()]
        if not instance or not mods:return
        if QMessageBox.question(self,"안전하게 제거",f"선택한 {len(mods)}개 파일을 trash로 이동할까요?")!=QMessageBox.StandardButton.Yes:return
        token=self._set_busy(True,"trash로 이동하는 중…")
        worker=Worker(self.trash.remove,instance,mods,name="trash-remove"); worker.signals.result.connect(self._trash_done); worker.signals.error.connect(self._task_error)
        self.workers.start(worker,lambda:self._finish_busy(token))

    def _trash_done(self, _paths) -> None:
        self._set_busy(False); self.show_installed()

    def validate_installed(self) -> None:
        instance=self.selected_instance()
        if not instance:return
        token=self._set_busy(True,"인스턴스 dependency 검사 중…")
        worker=Worker(lambda:validate_inventory(self.api,self.cache.inventory(self.api,instance)[0]),name="validate-instance")
        worker.signals.result.connect(self._validation_done); worker.signals.error.connect(self._task_error); self.workers.start(worker,lambda:self._finish_busy(token))

    def _validation_done(self, issues) -> None:
        self._set_busy(False); counts={k:sum(i.dependency_type==k for i in issues) for k in ("required","incompatible","unknown")}
        QMessageBox.information(self,"검사 결과",f"Missing required: {counts['required']}\nIncompatible: {counts['incompatible']}\nUnknown/Unverified: {counts['unknown']}")

    def _task_error(self, error: Exception) -> None:
        logger.error("task.error error=%r", error)
        self._set_busy(False)
        QMessageBox.warning(self,"작업 실패",str(error) or type(error).__name__); self.status_label.setText(f"오류: {error}")

    def _set_busy(self, busy: bool, text: str="", progress: bool=False) -> int:
        if busy:
            self._busy_token += 1
        self.busy=busy; self.progress.setVisible(progress); self.progress.setValue(0)
        for widget in (self.skip_button,self.maybe_button,self.install_button,self.apply_button,self.browse_button,self.instance_combo,self.version_edit,self.loader_combo,self.index_combo): widget.setEnabled(not busy)
        if text: self.status_label.setText(text)
        logger.info("ui.busy state=%s token=%d status=%s", busy, self._busy_token, text)
        return self._busy_token

    def _finish_busy(self, token: int) -> None:
        logger.info("ui.busy.finalize requested_token=%d current_token=%d busy=%s",
                    token, self._busy_token, self.busy)
        if self.busy and token == self._busy_token:
            self._set_busy(False)

    def open_web(self) -> None:
        if self.collection_view:self.open_selected_library()
        elif self.current_project(): webbrowser.open(self.current_project().project_url)

    def update_status(self) -> None:
        counts=[self.database.count_by_decision(d) for d in (Decision.SKIPPED,Decision.MAYBE,Decision.REVIEWED)]
        self.status_label.setText(f"Seen: {sum(counts):,}   Skipped: {counts[0]:,}   Maybe: {counts[1]:,}   Reviewed: {counts[2]:,}")

    def _set_controls(self, enabled: bool) -> None:
        for widget in (self.skip_button,self.maybe_button,self.install_button,self.apply_button): widget.setEnabled(enabled)

    def closeEvent(self,event) -> None:
        self.pool.waitForDone(3000); self.api.close(); self.database.close(); event.accept()


def main() -> None:
    log_path=configure_diagnostics(CONFIG.database_path.parent)
    logger.info("application.start diagnostic_log=%s", log_path)
    application=QApplication(sys.argv); window=MainWindow(); window.show(); sys.exit(application.exec())
