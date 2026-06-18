import os
from app_main import app, db, System, BenchmarkResult
from app.parser import parse_file

def seed():
    with app.app_context():
        for sys in System.query.all():
            db.session.delete(sys)
        db.session.commit()
        
        benchmarks_dir = "/home/system76/benchviz/benchmarks"
        # Load Mira
        parse_file(os.path.join(benchmarks_dir, "mira-r4-n3-llamacpp.xml"))
        parse_file(os.path.join(benchmarks_dir, "mira-r4-n3-onnx.xml"))
        
        # Clone it to simulate a second machine for UI testing
        mira = System.query.first()
        fake_sys = System(
            identifier="Star Labs Starfighter",
            chassis_version="v2",
            hardware='{"Processor": "Intel Core i9-13900H", "Memory": "64GB"}',
            software='{"OS": "Pop!_OS 22.04 LTS"}',
            user="automated_test",
            timestamp="2026-03-12"
        )
        db.session.add(fake_sys)
        db.session.commit()
        
        # Clone Results
        for res in BenchmarkResult.query.filter_by(system_id=mira.id).all():
            fake_res = BenchmarkResult(
                system_id=fake_sys.id,
                benchmark_id=res.benchmark_id,
                arguments=res.arguments,
                value=res.value * 0.8 if res.value else None,
                data_json=res.data_json
            )
            db.session.add(fake_res)
            
        db.session.commit()
        print(f"Total Systems in DB: {System.query.count()}")

if __name__ == "__main__":
    seed()
