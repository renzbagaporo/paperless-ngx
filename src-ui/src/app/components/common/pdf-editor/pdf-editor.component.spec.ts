import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http'
import { provideHttpClientTesting } from '@angular/common/http/testing'
import { ComponentFixture, TestBed } from '@angular/core/testing'
import { NgbActiveModal } from '@ng-bootstrap/ng-bootstrap'
import { NgxBootstrapIconsModule, allIcons } from 'ngx-bootstrap-icons'
import { DocumentService } from 'src/app/services/rest/document.service'
import { PdfEditorEditMode } from './pdf-editor-edit-mode'
import { PDFEditorComponent } from './pdf-editor.component'

describe('PDFEditorComponent', () => {
  let component: PDFEditorComponent
  let fixture: ComponentFixture<PDFEditorComponent>

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PDFEditorComponent, NgxBootstrapIconsModule.pick(allIcons)],
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
        { provide: NgbActiveModal, useValue: {} },
      ],
    }).compileComponents()
    fixture = TestBed.createComponent(PDFEditorComponent)
    component = fixture.componentInstance
    fixture.detectChanges()
  })

  it('should return correct operations with no changes', () => {
    component.pages = [
      { page: 1, rotate: 0, splitAfter: false },
      { page: 2, rotate: 0, splitAfter: false },
      { page: 3, rotate: 0, splitAfter: false },
    ]
    const ops = component.getOperations()
    expect(ops).toEqual([
      { page: 1, rotate: 0, doc: 0 },
      { page: 2, rotate: 0, doc: 0 },
      { page: 3, rotate: 0, doc: 0 },
    ])
  })

  it('should rotate, delete and reorder pages', () => {
    component.pages = [
      { page: 1, rotate: 0, splitAfter: false, selected: false },
      { page: 2, rotate: 0, splitAfter: false, selected: false },
    ]
    component.toggleSelection(0)
    component.rotateSelected(90)
    expect(component.pages[0].rotate).toBe(90)
    component.toggleSelection(0) // deselect
    component.toggleSelection(1)
    component.deleteSelected()
    expect(component.pages.length).toBe(1)
    component.pages.push({ page: 2, rotate: 0, splitAfter: false })
    component.drop({ previousIndex: 0, currentIndex: 1 } as any)
    expect(component.pages[0].page).toBe(2)
    component.rotate(0)
    expect(component.pages[0].rotate).toBe(90)
  })

  it('should handle empty pages array', () => {
    component.pages = []
    expect(component.getOperations()).toEqual([])
  })

  it('should increment doc index after splitAfter', () => {
    component.pages = [
      { page: 1, rotate: 0, splitAfter: true },
      { page: 2, rotate: 0, splitAfter: false },
      { page: 3, rotate: 0, splitAfter: true },
      { page: 4, rotate: 0, splitAfter: false },
    ]
    const ops = component.getOperations()
    expect(ops).toEqual([
      { page: 1, rotate: 0, doc: 0 },
      { page: 2, rotate: 0, doc: 1 },
      { page: 3, rotate: 0, doc: 1 },
      { page: 4, rotate: 0, doc: 2 },
    ])
  })

  it('should include rotations in operations', () => {
    component.pages = [
      { page: 1, rotate: 90, splitAfter: false },
      { page: 2, rotate: 180, splitAfter: true },
      { page: 3, rotate: 270, splitAfter: false },
    ]
    const ops = component.getOperations()
    expect(ops).toEqual([
      { page: 1, rotate: 90, doc: 0 },
      { page: 2, rotate: 180, doc: 0 },
      { page: 3, rotate: 270, doc: 1 },
    ])
  })

  it('should handle remove operation', () => {
    component.pages = [
      { page: 1, rotate: 0, splitAfter: false, selected: false },
      { page: 2, rotate: 0, splitAfter: false, selected: true },
      { page: 3, rotate: 0, splitAfter: false, selected: false },
    ]
    component.remove(1) // remove page 2
    expect(component.pages.length).toBe(2)
    expect(component.pages[0].page).toBe(1)
    expect(component.pages[1].page).toBe(3)
  })

  it('should toggle splitAfter correctly', () => {
    component.pages = [
      { page: 1, rotate: 0, splitAfter: false },
      { page: 2, rotate: 0, splitAfter: false },
    ]
    component.toggleSplit(0)
    expect(component.pages[0].splitAfter).toBeTruthy()
    component.toggleSplit(1)
    expect(component.pages[1].splitAfter).toBeTruthy()
  })

  it('should select and deselect all pages', () => {
    component.pages = [
      { page: 1, rotate: 0, splitAfter: false, selected: false },
      { page: 2, rotate: 0, splitAfter: false, selected: false },
    ]
    component.selectAll()
    expect(component.pages.every((p) => p.selected)).toBeTruthy()
    expect(component.hasSelection()).toBeTruthy()
    component.deselectAll()
    expect(component.pages.every((p) => !p.selected)).toBeTruthy()
    expect(component.hasSelection()).toBeFalsy()
  })

  it('should handle pdf loading and page generation', () => {
    const mockPdf = {
      numPages: 3,
      getPage: (pageNum: number) => Promise.resolve({ pageNumber: pageNum }),
    }
    component.pdfLoaded(mockPdf as any)
    expect(component.totalPages).toBe(3)
    expect(component.pages.length).toBe(3)
    expect(component.pages[0].page).toBe(1)
    expect(component.pages[1].page).toBe(2)
    expect(component.pages[2].page).toBe(3)
  })

  it('should include selected version in preview source when provided', () => {
    const documentService = TestBed.inject(DocumentService)
    const previewSpy = jest
      .spyOn(documentService, 'getPreviewUrl')
      .mockReturnValue('preview-version')
    component.documentID.set(3)
    component.versionID.set(10)

    expect(component.pdfSrc).toBe('preview-version')
    expect(previewSpy).toHaveBeenCalledWith(3, false, 10)
  })

  it('appends a scan without discarding page edits and switches to update mode', () => {
    component.editMode = PdfEditorEditMode.Create
    component.pages = [
      { page: 3, rotate: 90, splitAfter: false },
      { page: 1, rotate: 180, splitAfter: false },
    ]
    const file = new File(['scan'], 'notice.pdf', { type: 'application/pdf' })
    component.onAppendFileSelected({
      target: { files: [file], value: 'notice.pdf' },
    } as unknown as Event)
    expect(component.appendFile).toBe(file)
    expect(component.editMode).toBe(PdfEditorEditMode.Update)
    expect(component.getOperations()).toEqual([
      { page: 3, rotate: 90, doc: 0 },
      { page: 1, rotate: 180, doc: 0 },
    ])
    component.toggleSplit(0)
    expect(component.hasSplit()).toBe(false)
    component.removeAppendFile()
    expect(component.appendFile).toBeNull()
    expect(component.getOperations()).toEqual([
      { page: 3, rotate: 90, doc: 0 },
      { page: 1, rotate: 180, doc: 0 },
    ])
  })

  it('keeps split/create operations when an append is unavailable', () => {
    component.pages = [{ page: 1, rotate: 90, splitAfter: true }]
    component.editMode = PdfEditorEditMode.Create
    component.onAppendFileSelected({
      target: { files: [new File(['scan'], 'new.pdf')], value: 'new.pdf' },
    } as unknown as Event)
    expect(component.appendFile).toBeNull()
    expect(component.editMode).toBe(PdfEditorEditMode.Create)
    expect(component.getOperations()).toEqual([{ page: 1, rotate: 90, doc: 0 }])
  })

  it('requires deletion permission and explicit confirmation to remove earlier files', () => {
    component.pages = [{ page: 1, rotate: 0, splitAfter: false }]
    component.editMode = PdfEditorEditMode.Update
    component.configureRetention(true)
    const confirmed = jest.spyOn(component.confirmClicked, 'emit')
    component.confirm()
    expect(confirmed).not.toHaveBeenCalled()
    component.fileRemovalConfirmed = true
    component.confirm()
    expect(confirmed).toHaveBeenCalledTimes(1)
    component.configureRetention(false)
    expect(component.keepOnlyCurrentFile).toBe(false)
    expect(component.canSubmit).toBe(true)
    component.keepOnlyCurrentFile = true
    expect(component.canSubmit).toBe(false)
  })

  it('allows removing every old page when appending a scan, and blocks duplicate submissions', () => {
    component.editMode = PdfEditorEditMode.Update
    component.pages = []
    expect(component.canSubmit).toBe(false)
    component.appendFile = new File(['scan'], 'new.pdf')
    expect(component.canSubmit).toBe(true)
    expect(component.getOperations()).toEqual([])
    component.buttonsEnabled.set(false)
    expect(component.canSubmit).toBe(false)
  })
})
