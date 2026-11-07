module.exports = {
  apps: [
    {
      name: 'clonoth',
      script: 'main.py',
      interpreter: 'python',
      args: '--no-shell',
      cwd: __dirname,
      treekill: false,
      kill_timeout: 10000,
      autorestart: true,
    }
  ]
};
