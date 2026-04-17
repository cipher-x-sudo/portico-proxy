import React, { useState, useEffect } from 'react';
import Sidebar from './components/Sidebar';
import Header from './components/Header';
import Dashboard from './pages/Dashboard';
import Status from './pages/Status';
import Config from './pages/Config';
import Actions from './pages/Actions';
import './App.css';

function App() {
  const [activeTab, setActiveTab] = useState('dashboard');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(window.innerWidth <= 768);

  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth <= 768) {
        setIsSidebarCollapsed(true);
      } else {
        setIsSidebarCollapsed(false);
      }
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);
  
  // Basic mock state for header (can be updated from pages if needed)
  const isRunning = true; 

  const renderContent = () => {
    switch (activeTab) {
      case 'dashboard':
        return <Dashboard />;
      case 'status':
        return <Status />;
      case 'config':
        return <Config />;
      case 'actions':
        return <Actions />;
      default:
        return <Dashboard />;
    }
  };

  const getPageTitle = () => {
    switch (activeTab) {
      case 'dashboard': return 'Dashboard';
      case 'status': return 'System status';
      case 'config': return 'Configuration';
      case 'actions': return 'Logs & actions';
      default: return 'Dashboard';
    }
  };

  const handleTabClick = (tab) => {
    setActiveTab(tab);
    if (window.innerWidth <= 768) {
      setIsSidebarCollapsed(true);
    }
  };

  return (
    <div className={`app-layout ${isSidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
      <Sidebar activeTab={activeTab} setActiveTab={handleTabClick} isCollapsed={isSidebarCollapsed} setIsCollapsed={setIsSidebarCollapsed} />
      
      {/* Mobile backdrop */}
      <div 
        className={`sidebar-backdrop ${!isSidebarCollapsed ? 'visible' : ''}`}
        onClick={() => setIsSidebarCollapsed(true)}
      ></div>

      <main className="main-content">
        <Header title={getPageTitle()} isRunning={isRunning} toggleSidebar={() => setIsSidebarCollapsed(!isSidebarCollapsed)} />
        
        <div className="page-container animate-fade-in" key={activeTab}>
          {renderContent()}
        </div>
      </main>
    </div>
  );
}

export default App;
