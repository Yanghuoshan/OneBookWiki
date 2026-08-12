import { useState, useRef, type DragEvent, type ChangeEvent } from 'react';
import { uploadBook, UploadError } from '../data/bookListLoader';

type Props = {
  onUploaded: () => void;
};

const SUPPORTED_EXTENSIONS = [
  '.pdf', '.epub', '.mobi', '.azw', '.azw3',
  '.txt', '.doc', '.docx', '.html', '.htm',
];
const SUPPORTED_LABEL = 'PDF, EPUB, MOBI, AZW, TXT, DOC, DOCX, HTML';
const MAX_FILE_SIZE = 500 * 1024 * 1024; // 500 MB

function validateFile(file: File): string | null {
  const ext = '.' + file.name.split('.').pop()?.toLowerCase();
  if (!SUPPORTED_EXTENSIONS.includes(ext)) {
    return `Unsupported format. Supported: ${SUPPORTED_LABEL}`;
  }
  if (file.size > MAX_FILE_SIZE) {
    return 'File too large. Maximum size: 500 MB.';
  }
  if (file.size === 0) {
    return 'File is empty.';
  }
  return null;
}

export default function UploadZone({ onUploaded }: Props) {
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function handleFile(file: File) {
    const validationError = validateFile(file);
    if (validationError) {
      setError(validationError);
      setSuccess(null);
      return;
    }
    setError(null);
    setSuccess(null);
    setUploading(true);
    setProgress(0);
    try {
      const result = await uploadBook(file, (pct) => setProgress(pct));
      setSuccess(`"${result.title}" uploaded! Processing will start automatically.`);
      onUploaded();
    } catch (e) {
      setError(e instanceof UploadError ? e.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  }

  function onDragOver(e: DragEvent) {
    e.preventDefault();
    setDragActive(true);
  }

  function onDragLeave(e: DragEvent) {
    e.preventDefault();
    setDragActive(false);
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragActive(false);
    if (e.dataTransfer.files.length) {
      handleFile(e.dataTransfer.files[0]);
    }
  }

  function onFileChange(e: ChangeEvent<HTMLInputElement>) {
    if (e.target.files?.length) {
      handleFile(e.target.files[0]);
      // Reset input so the same file can be re-selected
      e.target.value = '';
    }
  }

  function triggerFileInput() {
    fileInputRef.current?.click();
  }

  return (
    <section className="upload-section">
      <div
        className={`upload-zone ${dragActive ? 'upload-zone--active' : ''} ${uploading ? 'upload-zone--uploading' : ''}`}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        onClick={triggerFileInput}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            triggerFileInput();
          }
        }}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept={SUPPORTED_EXTENSIONS.join(',')}
          onChange={onFileChange}
          hidden
        />
        {uploading ? (
          <div className="upload-progress">
            <div className="upload-progress-bar" style={{ width: `${progress}%` }} />
            <span>Uploading... {progress}%</span>
          </div>
        ) : (
          <>
            <div className="upload-icon">+</div>
            <p className="upload-text">
              Drop an ebook file here, or click to select
            </p>
            <p className="upload-hint">
              Supports {SUPPORTED_LABEL} (max 500 MB)
            </p>
          </>
        )}
      </div>
      {error && (
        <div className="upload-message upload-message--error">{error}</div>
      )}
      {success && (
        <div className="upload-message upload-message--success">{success}</div>
      )}
    </section>
  );
}
