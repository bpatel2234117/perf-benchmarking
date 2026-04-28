import { useState } from 'react';
import { BrowserRouter as Router, Route, Routes, Navigate } from 'react-router-dom';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Logs from './pages/Logs';
import Workflow from './pages/Workflow';
import Environments from './pages/Environments';
import LegacyToolPage from './pages/LegacyToolPage';
import Navbar from './components/Navbar';

const App = () => {
  const [token, setToken] = useState<string | null>(localStorage.getItem('token'));

  const handleSetToken = (newToken: string | null) => {
    if (newToken) {
      localStorage.setItem('token', newToken);
    } else {
      localStorage.removeItem('token');
    }
    setToken(newToken);
  };

  const routesInner = (
    <Routes>
      <Route path="/login" element={!token ? <Login setToken={handleSetToken} /> : <Navigate to="/" />} />
      <Route path="/" element={token ? <Dashboard token={token} /> : <Navigate to="/login" />} />
      <Route path="/logs" element={token ? <Logs token={token} /> : <Navigate to="/login" />} />
      <Route path="/workflow" element={token ? <Workflow token={token} /> : <Navigate to="/login" />} />
      <Route path="/environments" element={token ? <Environments token={token} /> : <Navigate to="/login" />} />
      <Route path="/tools/:slug" element={token ? <LegacyToolPage /> : <Navigate to="/login" />} />
    </Routes>
  );

  return (
    <Router>
      {token ? (
        <Navbar onLogout={() => handleSetToken(null)}>{routesInner}</Navbar>
      ) : (
        <div className="main-content main-content-login">{routesInner}</div>
      )}
    </Router>
  );
};

export default App;

