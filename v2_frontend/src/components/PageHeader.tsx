import React from 'react';
import { LucideIcon } from 'lucide-react';

interface PageHeaderProps {
    icon: LucideIcon;
    title: string;
    subtitle?: string;
}

const PageHeader: React.FC<PageHeaderProps> = ({ icon: Icon, title, subtitle }) => {
    return (
        <div
            style={{
                background: 'linear-gradient(135deg, #8B5CF6 0%, #6366F1 100%)',
                padding: '1.2rem 1.5rem',
                borderRadius: '12px',
                marginBottom: '1.5rem',
                color: 'white',
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
            }}
        >
            <Icon size={28} style={{ flexShrink: 0 }} />
            <div>
                <h2 style={{ fontSize: '1.3rem', fontWeight: 700, letterSpacing: '-0.5px', margin: 0, color: 'white' }}>
                    {title}
                </h2>
                {subtitle && (
                    <p style={{ fontSize: '0.85rem', margin: '0.25rem 0 0', color: 'rgba(255, 255, 255, 0.9)' }}>
                        {subtitle}
                    </p>
                )}
            </div>
        </div>
    );
};

export default PageHeader;
