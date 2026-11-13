import {
  CdkDragDrop,
  DragDropModule,
  moveItemInArray,
} from '@angular/cdk/drag-drop'
import { ChangeDetectorRef, Component, inject, signal } from '@angular/core'
import { FormsModule } from '@angular/forms'
import { NgbActiveModal, NgbModal } from '@ng-bootstrap/ng-bootstrap'
import { NgxBootstrapIconsModule } from 'ngx-bootstrap-icons'
import { SETTINGS_KEYS } from 'src/app/data/ui-settings'
import { DocumentService } from 'src/app/services/rest/document.service'
import { SettingsService } from 'src/app/services/settings.service'
import { ConfirmDialogComponent } from '../confirm-dialog/confirm-dialog.component'
import { PngxPdfViewerComponent } from '../pdf-viewer/pdf-viewer.component'
import {
  PdfRenderMode,
  PngxPdfDocumentProxy,
} from '../pdf-viewer/pdf-viewer.types'
import { ScanDialogComponent } from '../scan-dialog/scan-dialog.component'
import { PdfEditorEditMode } from './pdf-editor-edit-mode'

interface PageOperation {
  page: number
  rotate: number
  splitAfter: boolean
  selected?: boolean
  loaded?: boolean
}

@Component({
  selector: 'pngx-pdf-editor',
  templateUrl: './pdf-editor.component.html',
  styleUrl: './pdf-editor.component.scss',
  imports: [
    DragDropModule,
    FormsModule,
    NgxBootstrapIconsModule,
    PngxPdfViewerComponent,
  ],
})
export class PDFEditorComponent extends ConfirmDialogComponent {
  PdfRenderMode = PdfRenderMode
  public PdfEditorEditMode = PdfEditorEditMode

  private documentService = inject(DocumentService)
  private readonly settingsService = inject(SettingsService)
  activeModal: NgbActiveModal = inject(NgbActiveModal)
  private modalService = inject(NgbModal)
  private changeDetector = inject(ChangeDetectorRef)
  readonly scanningEnabled = this.settingsService.getSignal<boolean>(
    SETTINGS_KEYS.SCANNING_ENABLED
  )

  readonly documentID = signal<number>(undefined)
  readonly versionID = signal<number>(undefined)
  /** A PDF to edit instead of a stored document, such as a new scan. */
  readonly sourceUrl = signal<string>(undefined)
  /**
   * Only reorder, rotate and remove pages: no appending, extracting,
   * splitting or document storage choices.
   */
  readonly pagesOnly = signal(false)

  pages: PageOperation[] = []
  totalPages = 0
  editMode: PdfEditorEditMode = this.settingsService.get(
    SETTINGS_KEYS.PDF_EDITOR_DEFAULT_EDIT_MODE
  )
  deleteOriginal: boolean = false
  includeMetadata: boolean = true
  canRemoveEarlierFiles = false
  keepOnlyCurrentFile = false
  fileRemovalConfirmed = false
  appendFile: File | null = null
  remoteOcr = false
  extractionMode: 'extract' | 'move' | null = null
  extractedTitle = ''
  extractedAsn: number | null = null

  getSelectedOperations() {
    return this.pages
      .filter((page) => page.selected)
      .map((page) => ({ page: page.page, rotate: page.rotate }))
  }

  configureRetention(canDelete: boolean) {
    this.canRemoveEarlierFiles = canDelete
    this.keepOnlyCurrentFile = canDelete
  }

  onAppendFileSelected(event: Event) {
    const input = event.target as HTMLInputElement
    const file = input.files?.[0]
    input.value = ''
    this.useAppendFile(file)
  }

  get canAppend(): boolean {
    return !this.hasSplit() && !this.extractionMode && this.buttonsEnabled()
  }

  private useAppendFile(file: File | undefined) {
    if (!file || !this.canAppend) return
    this.appendFile = file
    this.editMode = PdfEditorEditMode.Update
  }

  scanAppendFile() {
    if (!this.canAppend) return
    const modal = this.modalService.open(ScanDialogComponent, {
      backdrop: 'static',
      // Escape would close the dialog with its scan still running.
      keyboard: false,
      size: 'lg',
    })
    modal.componentInstance.title = $localize`Scan pages to append`
    modal.componentInstance.finishCaption = $localize`Use scan`
    modal.result.then(
      (file: File) => {
        this.useAppendFile(file)
        this.changeDetector.markForCheck()
      },
      () => {}
    )
  }

  removeAppendFile() {
    this.appendFile = null
  }

  get canSubmit(): boolean {
    if (this.pagesOnly()) {
      return this.buttonsEnabled() && this.pages.length > 0
    }
    if (this.extractionMode) {
      return (
        this.buttonsEnabled() &&
        this.hasSelection() &&
        !this.appendFile &&
        (this.extractionMode !== 'move' ||
          (this.getSelectedOperations().length < this.totalPages &&
            (!this.keepOnlyCurrentFile ||
              (this.canRemoveEarlierFiles && this.fileRemovalConfirmed))))
      )
    }
    return (
      this.buttonsEnabled() &&
      (this.pages.length > 0 || !!this.appendFile) &&
      !(
        this.appendFile &&
        (this.hasSplit() || this.editMode !== PdfEditorEditMode.Update)
      ) &&
      !(
        this.editMode === PdfEditorEditMode.Update &&
        this.keepOnlyCurrentFile &&
        (!this.canRemoveEarlierFiles || !this.fileRemovalConfirmed)
      ) &&
      !(
        this.editMode === PdfEditorEditMode.Create &&
        this.deleteOriginal &&
        !this.canRemoveEarlierFiles
      )
    )
  }

  override confirm() {
    if (this.canSubmit) super.confirm()
  }

  get pdfSrc(): string {
    if (this.sourceUrl()) return this.sourceUrl()
    return this.documentService.getPreviewUrl(
      this.documentID(),
      false,
      this.versionID()
    )
  }

  pdfLoaded(pdf: PngxPdfDocumentProxy) {
    this.totalPages = pdf.numPages
    this.pages = Array.from({ length: this.totalPages }, (_, i) => ({
      page: i + 1,
      rotate: 0,
      splitAfter: false,
      selected: false,
      loaded: false,
    }))
  }

  toggleSelection(i: number) {
    this.pages[i].selected = !this.pages[i].selected
  }

  rotate(i: number, counterclockwise: boolean = false) {
    this.pages[i].rotate =
      (this.pages[i].rotate + (counterclockwise ? -90 : 90) + 360) % 360
  }

  rotateSelected(dir: number) {
    for (let p of this.pages) {
      if (p.selected) {
        p.rotate = (p.rotate + dir + 360) % 360
      }
    }
  }

  remove(i: number) {
    this.pages.splice(i, 1)
  }

  toggleSplit(i: number) {
    if (this.appendFile || this.pagesOnly()) return
    this.pages[i].splitAfter = !this.pages[i].splitAfter
    if (this.pages[i].splitAfter) {
      // force create mode
      this.editMode = PdfEditorEditMode.Create
    }
  }

  selectAll() {
    this.pages.forEach((p) => (p.selected = true))
  }

  deselectAll() {
    this.pages.forEach((p) => (p.selected = false))
  }

  deleteSelected() {
    this.pages = this.pages.filter((p) => !p.selected)
  }

  hasSelection(): boolean {
    return this.pages.some((p) => p.selected)
  }

  hasSplit(): boolean {
    return this.pages.some((p) => p.splitAfter)
  }

  drop(event: CdkDragDrop<PageOperation[]>) {
    moveItemInArray(this.pages, event.previousIndex, event.currentIndex)
  }

  getOperations() {
    return this.pages.map((p, idx) => ({
      page: p.page,
      rotate: p.rotate,
      doc: this.computeDocIndex(idx),
    }))
  }

  private computeDocIndex(index: number): number {
    let docIndex = 0
    for (let i = 0; i <= index; i++) {
      if (this.pages[i].splitAfter && i < index) docIndex++
    }
    return docIndex
  }
}
