import React from 'react';

export interface PaginationBarProps {
  /** 0-based page index */
  page: number;
  pageSize: number;
  totalItems: number;
  onPageChange: (nextPage: number) => void;
  ariaLabel?: string;
}

/**
 * Lightweight prev/next + range label for server- or client-paginated lists.
 */
const PaginationBar: React.FC<PaginationBarProps> = ({
  page,
  pageSize,
  totalItems,
  onPageChange,
  ariaLabel = 'Pagination',
}) => {
  if (totalItems <= 0) return null;

  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
  const clampedPage = Math.min(Math.max(0, page), totalPages - 1);
  const startIdx = clampedPage * pageSize + 1;
  const endIdx = Math.min(totalItems, (clampedPage + 1) * pageSize);
  const canPrev = clampedPage > 0;
  const canNext = clampedPage < totalPages - 1;

  return (
    <div
      role="navigation"
      aria-label={ariaLabel}
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: '0.75rem',
        padding: '0.65rem 0 0',
        fontSize: '0.82rem',
        color: 'var(--text-secondary)',
      }}
    >
      <span>
        Showing <strong style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{startIdx}</strong>–
        <strong style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{endIdx}</strong> of{' '}
        <strong style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{totalItems}</strong>
      </span>
      <span style={{ display: 'inline-flex', gap: 6 }}>
        <button
          type="button"
          disabled={!canPrev}
          onClick={() => onPageChange(Math.max(0, clampedPage - 1))}
          style={{
            padding: '4px 10px',
            borderRadius: 6,
            border: '1px solid var(--surface-border)',
            background: canPrev ? 'var(--surface-alt, rgba(0,0,0,0.04))' : 'transparent',
            cursor: canPrev ? 'pointer' : 'not-allowed',
            opacity: canPrev ? 1 : 0.45,
            fontFamily: 'inherit',
            fontSize: '0.82rem',
          }}
        >
          Previous
        </button>
        <button
          type="button"
          disabled={!canNext}
          onClick={() => onPageChange(Math.min(totalPages - 1, clampedPage + 1))}
          style={{
            padding: '4px 10px',
            borderRadius: 6,
            border: '1px solid var(--surface-border)',
            background: canNext ? 'var(--surface-alt, rgba(0,0,0,0.04))' : 'transparent',
            cursor: canNext ? 'pointer' : 'not-allowed',
            opacity: canNext ? 1 : 0.45,
            fontFamily: 'inherit',
            fontSize: '0.82rem',
          }}
        >
          Next
        </button>
      </span>
    </div>
  );
};

export default PaginationBar;
