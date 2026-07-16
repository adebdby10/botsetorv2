module.exports = {
  apps: [{
    name: 'botsetor',
    script: 'run.py',
    cwd: __dirname,
    interpreter: '.venv/bin/python',
    log_file: 'logs/botsetor.log',
    out_file: 'logs/botsetor-out.log',
    error_file: 'logs/botsetor-error.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    env: {
      PYTHONUNBUFFERED: '1',
    },
  }]
};
