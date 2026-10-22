import type { ThemeConfig } from 'antd';

export const appTheme: ThemeConfig = {
  token: {
    colorPrimary: '#1d4ed8',
    colorInfo: '#1d4ed8',
    colorSuccess: '#047857',
    colorWarning: '#b45309',
    colorError: '#dc2626',
    colorText: '#0f172a',
    colorTextSecondary: '#475569',
    colorBgLayout: '#f6f8fb',
    colorBgContainer: '#ffffff',
    colorBorder: '#e2e8f0',
    colorBorderSecondary: '#edf2f7',
    borderRadius: 8,
    borderRadiusLG: 8,
    borderRadiusSM: 6,
    boxShadow: '0 1px 2px rgba(15, 23, 42, 0.04)',
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans', sans-serif",
    fontSize: 14,
    controlHeight: 34,
    controlHeightSM: 28,
    controlHeightLG: 40,
  },
  components: {
    Card: {
      headerHeightSM: 40,
      paddingSM: 14,
    },
    Table: {
      headerBg: '#f8fafc',
      headerColor: '#334155',
      rowHoverBg: '#f8fbff',
      cellPaddingBlockSM: 8,
      cellPaddingInlineSM: 10,
    },
    Menu: {
      darkItemBg: '#07111f',
      darkSubMenuItemBg: '#07111f',
      darkItemSelectedBg: '#1d4ed8',
      darkItemHoverBg: '#10213a',
    },
    Layout: {
      siderBg: '#07111f',
      headerBg: '#ffffff',
    },
    Button: {
      primaryShadow: 'none',
    },
  },
};
